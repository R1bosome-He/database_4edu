from __future__ import annotations

import json
from bisect import bisect_left
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .catalog import Catalog
from .models import TableSchema


@dataclass
class BPlusNode:
    leaf: bool
    keys: list[Any] = field(default_factory=list)
    children: list["BPlusNode"] = field(default_factory=list)
    values: list[list[str]] = field(default_factory=list)
    next: "BPlusNode | None" = None


class BPlusTree:
    """Small in-memory B+ tree used by the index manager."""

    def __init__(self, order: int = 8) -> None:
        if order < 3:
            raise ValueError("B+ tree order must be at least 3")
        self.order = order
        self.root = BPlusNode(leaf=True)

    def insert(self, key: Any, rid: str) -> None:
        leaf = self._find_leaf(key)
        pos = bisect_left(leaf.keys, key)
        if pos < len(leaf.keys) and leaf.keys[pos] == key:
            if rid not in leaf.values[pos]:
                leaf.values[pos].append(rid)
            return
        leaf.keys.insert(pos, key)
        leaf.values.insert(pos, [rid])
        if len(leaf.keys) > self.order:
            self._split_leaf(leaf)

    def search_equal(self, key: Any) -> list[str]:
        leaf = self._find_leaf(key)
        pos = bisect_left(leaf.keys, key)
        if pos < len(leaf.keys) and leaf.keys[pos] == key:
            return list(leaf.values[pos])
        return []

    def search_range(self, operator: str, key: Any) -> list[str]:
        result: list[str] = []
        leaf = self._leftmost_leaf()
        while leaf:
            for current_key, rids in zip(leaf.keys, leaf.values):
                if _compare(current_key, operator, key):
                    result.extend(rids)
            leaf = leaf.next
        return result

    def items(self) -> list[tuple[Any, list[str]]]:
        entries: list[tuple[Any, list[str]]] = []
        leaf = self._leftmost_leaf()
        while leaf:
            entries.extend((key, list(values)) for key, values in zip(leaf.keys, leaf.values))
            leaf = leaf.next
        return entries

    @classmethod
    def from_entries(cls, entries: Iterable[tuple[Any, list[str]]], order: int = 8) -> "BPlusTree":
        tree = cls(order=order)
        for key, rids in entries:
            for rid in rids:
                tree.insert(key, rid)
        return tree

    def _find_leaf(self, key: Any) -> BPlusNode:
        node = self.root
        while not node.leaf:
            index = 0
            while index < len(node.keys) and key >= node.keys[index]:
                index += 1
            node = node.children[index]
        return node

    def _leftmost_leaf(self) -> BPlusNode:
        node = self.root
        while not node.leaf:
            node = node.children[0]
        return node

    def _split_leaf(self, leaf: BPlusNode) -> None:
        mid = (len(leaf.keys) + 1) // 2
        right = BPlusNode(leaf=True)
        right.keys = leaf.keys[mid:]
        right.values = leaf.values[mid:]
        leaf.keys = leaf.keys[:mid]
        leaf.values = leaf.values[:mid]
        right.next = leaf.next
        leaf.next = right
        self._insert_in_parent(leaf, right.keys[0], right)

    def _insert_in_parent(self, left: BPlusNode, key: Any, right: BPlusNode) -> None:
        if left is self.root:
            self.root = BPlusNode(leaf=False, keys=[key], children=[left, right])
            return
        parent = self._find_parent(self.root, left)
        if parent is None:
            raise RuntimeError("B+ tree parent lookup failed")
        child_index = parent.children.index(left) + 1
        parent.keys.insert(child_index - 1, key)
        parent.children.insert(child_index, right)
        if len(parent.keys) > self.order:
            self._split_internal(parent)

    def _split_internal(self, node: BPlusNode) -> None:
        mid = len(node.keys) // 2
        promote = node.keys[mid]
        right = BPlusNode(leaf=False)
        right.keys = node.keys[mid + 1 :]
        right.children = node.children[mid + 1 :]
        node.keys = node.keys[:mid]
        node.children = node.children[: mid + 1]
        self._insert_in_parent(node, promote, right)

    def _find_parent(self, current: BPlusNode, child: BPlusNode) -> BPlusNode | None:
        if current.leaf:
            return None
        if child in current.children:
            return current
        for candidate in current.children:
            parent = self._find_parent(candidate, child)
            if parent:
                return parent
        return None


class IndexManager:
    """Builds, persists, and probes B+ tree indexes."""

    def __init__(self, root: Path, catalog: Catalog) -> None:
        self.root = root
        self.catalog = catalog
        self.index_dir = root / "indexes"
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.cache: dict[tuple[str, str], BPlusTree] = {}

    def create_index(
        self,
        table: str,
        column: str,
        index_name: str,
        scan_records: Callable[[str], Iterable[dict[str, Any]]],
    ) -> None:
        schema = self.catalog.table(table)
        schema.column(column)
        tree = BPlusTree()
        for record in scan_records(schema.name):
            tree.insert(record[column.lower()], record["_rid"])
        self.catalog.add_index(schema.name, index_name, column)
        self.cache[(schema.name, column.lower())] = tree
        self._save(schema.name, column.lower(), tree)

    def insert_record(self, schema: TableSchema, record: dict[str, Any]) -> None:
        for index in schema.indexes:
            tree = self.load(schema.name, index.column)
            tree.insert(record[index.column], record["_rid"])
            self._save(schema.name, index.column, tree)

    def rebuild_table(
        self,
        table: str,
        scan_records: Callable[[str], Iterable[dict[str, Any]]],
    ) -> None:
        schema = self.catalog.table(table)
        for index in schema.indexes:
            tree = BPlusTree()
            for record in scan_records(schema.name):
                tree.insert(record[index.column], record["_rid"])
            self.cache[(schema.name, index.column)] = tree
            self._save(schema.name, index.column, tree)

    def search(self, table: str, column: str, operator: str, value: Any) -> list[str]:
        tree = self.load(table, column)
        if operator == "=":
            return tree.search_equal(value)
        if operator in {"<", "<=", ">", ">="}:
            return tree.search_range(operator, value)
        raise ValueError(f"Index does not support operator: {operator}")

    def load(self, table: str, column: str) -> BPlusTree:
        key = (table.lower(), column.lower())
        if key in self.cache:
            return self.cache[key]
        path = self._path(*key)
        if not path.exists():
            raise KeyError(f"Index not found: {table}.{column}")
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        entries = [(entry["key"], entry["rids"]) for entry in data.get("entries", [])]
        tree = BPlusTree.from_entries(entries)
        self.cache[key] = tree
        return tree

    def _save(self, table: str, column: str, tree: BPlusTree) -> None:
        path = self._path(table, column)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(
                {
                    "table": table.lower(),
                    "column": column.lower(),
                    "type": "b_plus_tree",
                    "entries": [{"key": key, "rids": rids} for key, rids in tree.items()],
                },
                fh,
                ensure_ascii=False,
                indent=2,
            )
        tmp.replace(path)

    def _path(self, table: str, column: str) -> Path:
        return self.index_dir / f"{table.lower()}__{column.lower()}.idx.json"


def _compare(left: Any, operator: str, right: Any) -> bool:
    if operator == "<":
        return left < right
    if operator == "<=":
        return left <= right
    if operator == ">":
        return left > right
    if operator == ">=":
        return left >= right
    if operator == "=":
        return left == right
    if operator == "!=":
        return left != right
    raise ValueError(f"Unknown operator: {operator}")
