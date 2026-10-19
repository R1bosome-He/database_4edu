"""B+ tree with linked leaves, duplicate keys and real delete rebalancing.

Entries are (SQL key, page_id, slot_id); the RID breaks duplicate-key ties.
NULL is omitted by IndexManager, since ordinary comparisons do not match NULL.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from ..c_api import DBError, RangeSpec


@dataclass
class Node:
    id: int
    leaf: bool
    keys: list[tuple] = field(default_factory=list)
    children: list[int] = field(default_factory=list)
    parent: int | None = None
    next: int | None = None


class BPlusTree:
    def __init__(self, order: int = 8) -> None:
        if order < 3:
            raise ValueError("order must be >= 3")
        self.order = order
        self.nodes: dict[int, Node] = {}
        self.next_id = 1
        self.root = self._new(True).id

    def _new(self, leaf: bool, parent=None) -> Node:
        node = Node(self.next_id, leaf, parent=parent)
        self.nodes[node.id] = node
        self.next_id += 1
        return node

    def _leaf(self, entry: tuple) -> Node:
        node = self.nodes[self.root]
        while not node.leaf:
            node = self.nodes[node.children[bisect_right(node.keys, entry)]]
        return node

    def _refresh(self, node_id=None) -> tuple | None:
        node = self.nodes[self.root if node_id is None else node_id]
        if node.leaf:
            return node.keys[0] if node.keys else None
        minima = [self._refresh(child) for child in node.children]
        node.keys = minima[1:]
        return minima[0]

    def insert(self, key, rid) -> None:
        if key is None:
            return
        entry = (key, rid.page_id, rid.slot_id)
        leaf = self._leaf(entry)
        pos = bisect_left(leaf.keys, entry)
        if pos < len(leaf.keys) and leaf.keys[pos] == entry:
            return
        leaf.keys.insert(pos, entry)
        if len(leaf.keys) > self.order:
            self._split(leaf)
        self._refresh()

    def _split(self, left: Node) -> None:
        right = self._new(left.leaf, left.parent)
        if left.leaf:
            mid = (len(left.keys) + 1) // 2
            right.keys, left.keys = left.keys[mid:], left.keys[:mid]
            right.next, left.next = left.next, right.id
        else:
            mid = (len(left.children) + 1) // 2
            right.children, left.children = left.children[mid:], left.children[:mid]
            for child in right.children:
                self.nodes[child].parent = right.id
        if left.parent is None:
            parent = self._new(False)
            parent.children = [left.id, right.id]
            left.parent = right.parent = parent.id
            self.root = parent.id
        else:
            parent = self.nodes[left.parent]
            parent.children.insert(parent.children.index(left.id) + 1, right.id)
            if len(parent.children) > self.order + 1:
                self._split(parent)

    def delete(self, key, rid) -> None:
        if key is None:
            return
        entry = (key, rid.page_id, rid.slot_id)
        node = self._leaf(entry)
        pos = bisect_left(node.keys, entry)
        if pos == len(node.keys) or node.keys[pos] != entry:
            raise DBError("INDEX_ENTRY_MISSING", "Index is inconsistent with table")
        node.keys.pop(pos)
        self._rebalance(node)
        self._refresh()

    def _minimum(self, node: Node) -> int:
        return (self.order + 1) // 2 if node.leaf else (self.order + 2) // 2

    @staticmethod
    def _size(node: Node) -> int:
        return len(node.keys) if node.leaf else len(node.children)

    def _rebalance(self, node: Node) -> None:
        if node.parent is None:
            if not node.leaf and len(node.children) == 1:
                self.root = node.children[0]
                self.nodes[self.root].parent = None
                del self.nodes[node.id]
            return
        if self._size(node) >= self._minimum(node):
            return
        parent = self.nodes[node.parent]
        pos = parent.children.index(node.id)
        left = self.nodes[parent.children[pos - 1]] if pos else None
        right = self.nodes[parent.children[pos + 1]] if pos + 1 < len(parent.children) else None
        for sibling, from_left in ((left, True), (right, False)):
            if sibling and self._size(sibling) > self._minimum(sibling):
                if node.leaf:
                    item = sibling.keys.pop(-1 if from_left else 0)
                    node.keys.insert(0 if from_left else len(node.keys), item)
                else:
                    child = sibling.children.pop(-1 if from_left else 0)
                    node.children.insert(0 if from_left else len(node.children), child)
                    self.nodes[child].parent = node.id
                return
        if left:
            survivor, removed = left, node
        elif right:
            survivor, removed = node, right
        else:
            raise DBError("INDEX_CORRUPTION", "Underfull node has no sibling")
        if node.leaf:
            survivor.keys.extend(removed.keys)
            survivor.next = removed.next
        else:
            survivor.children.extend(removed.children)
            for child in removed.children:
                self.nodes[child].parent = survivor.id
        parent.children.remove(removed.id)
        del self.nodes[removed.id]
        self._rebalance(parent)

    def search(self, spec: RangeSpec):
        if spec.lower is None:
            node = self.nodes[self.root]
            while not node.leaf:
                node = self.nodes[node.children[0]]
        else:
            # RID lower bound includes every duplicate of the lower key.
            node = self._leaf((spec.lower, -1, -1))
        while True:
            for value, page, slot in node.keys:
                if spec.lower is not None and (value < spec.lower or (value == spec.lower and not spec.lower_inclusive)):
                    continue
                if spec.upper is not None and (value > spec.upper or (value == spec.upper and not spec.upper_inclusive)):
                    return
                yield value, page, slot
            if node.next is None:
                return
            node = self.nodes[node.next]

    def dump(self) -> dict[int, dict]:
        return {n.id: dict(leaf=n.leaf, keys=[list(k) for k in n.keys], children=list(n.children),
                           parent=n.parent, next=n.next) for n in self.nodes.values()}

    @classmethod
    def load(cls, order, root, next_id, payloads):
        tree = cls(order)
        tree.root, tree.next_id = root, next_id
        tree.nodes = {int(i): Node(int(i), p["leaf"], [tuple(k) for k in p["keys"]],
                                   list(p["children"]), p["parent"], p["next"]) for i, p in payloads.items()}
        tree.validate()
        return tree

    def validate(self) -> None:
        seen, leaves, depths = set(), [], set()
        def visit(node_id, parent, depth):
            if node_id in seen or node_id not in self.nodes:
                raise DBError("INDEX_CORRUPTION", "Missing/cyclic B+ node")
            seen.add(node_id)
            node = self.nodes[node_id]
            if node.parent != parent or node.keys != sorted(node.keys):
                raise DBError("INDEX_CORRUPTION", "Invalid parent or key order")
            if parent is not None and self._size(node) < self._minimum(node):
                raise DBError("INDEX_CORRUPTION", "Underfull B+ node")
            if len(node.keys) > self.order:
                raise DBError("INDEX_CORRUPTION", "Overflowing B+ node")
            if node.leaf:
                leaves.append(node)
                depths.add(depth)
                return node.keys[0] if node.keys else None
            if len(node.children) != len(node.keys) + 1:
                raise DBError("INDEX_CORRUPTION", "Bad child count")
            minima = [visit(c, node.id, depth + 1) for c in node.children]
            if node.keys != minima[1:]:
                raise DBError("INDEX_CORRUPTION", "Bad separators")
            return minima[0]
        visit(self.root, None, 0)
        if seen != set(self.nodes) or len(depths) != 1:
            raise DBError("INDEX_CORRUPTION", "Unreachable nodes or uneven leaves")
        all_keys = []
        for index, leaf in enumerate(leaves):
            expected = leaves[index + 1].id if index + 1 < len(leaves) else None
            if leaf.next != expected:
                raise DBError("INDEX_CORRUPTION", "Broken leaf chain")
            all_keys.extend(leaf.keys)
        if all_keys != sorted(all_keys) or len(all_keys) != len(set(all_keys)):
            raise DBError("INDEX_CORRUPTION", "Duplicate RID or unordered leaves")
