from __future__ import annotations

import operator
import time
from typing import Any, Iterable

from .buffer import BufferManager
from .catalog import Catalog
from .index import IndexManager
from .models import Condition, QueryResult, SelectStatement
from .planner import PlanNode, QueryPlanner


OPS = {
    "=": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


class QueryExecutor:
    def __init__(
        self,
        catalog: Catalog,
        buffer: BufferManager,
        indexes: IndexManager,
        planner: QueryPlanner,
    ) -> None:
        self.catalog = catalog
        self.buffer = buffer
        self.indexes = indexes
        self.planner = planner

    def explain(self, query: SelectStatement, optimize: bool) -> str:
        return self.planner.build(query, optimize).format()

    def execute_select(self, query: SelectStatement, optimize: bool) -> QueryResult:
        self.buffer.clear()
        self.buffer.reset_metrics()
        plan = self.planner.build(query, optimize)
        start = time.perf_counter()
        rows = self._execute_node(plan)
        self.buffer.flush_all()
        elapsed_ms = (time.perf_counter() - start) * 1000
        metrics = self.buffer.snapshot_metrics()
        metrics.update(
            {
                "elapsed_ms": round(elapsed_ms, 3),
                "rows": len(rows),
                "optimizer": "on" if optimize else "off",
            }
        )
        columns = _derive_columns(rows, query.columns)
        return QueryResult(
            kind="table",
            columns=columns,
            rows=rows,
            plan=plan.format(),
            metrics=metrics,
        )

    def _execute_node(self, node: PlanNode) -> list[dict[str, Any]]:
        if node.op == "Projection":
            rows = self._execute_node(node.children[0])
            return [self._project(row, node.columns) for row in rows]
        if node.op == "Filter":
            rows = self._execute_node(node.children[0])
            return [row for row in rows if all(_eval_condition(row, c) for c in node.conditions)]
        if node.op == "Sort":
            rows = self._execute_node(node.children[0])
            for order in reversed(node.order_by):
                rows.sort(
                    key=lambda row, column=order.column: _sort_key(_resolve(row, column)),
                    reverse=order.descending,
                )
            return rows
        if node.op == "SeqScan":
            return list(self._seq_scan(node.table or ""))
        if node.op == "IndexScan":
            assert node.table and node.condition and node.index_column
            return list(self._index_scan(node.table, node.index_column, node.condition))
        if node.op == "NestedLoopJoin":
            return self._nested_loop_join(node)
        if node.op == "HashJoin":
            return self._hash_join(node)
        raise ValueError(f"Unsupported plan node: {node.op}")

    def _seq_scan(self, table: str) -> Iterable[dict[str, Any]]:
        schema = self.catalog.table(table)
        for page_id in range(schema.page_count):
            page = self.buffer.get_page(schema.name, page_id)
            for record in page.records:
                yield _qualify(schema.name, record)

    def _index_scan(self, table: str, column: str, condition: Condition) -> Iterable[dict[str, Any]]:
        schema = self.catalog.table(table)
        rids = self.indexes.search(schema.name, column, condition.operator, condition.value)
        for rid in rids:
            page_id_text, slot_text = rid.split(":", 1)
            page = self.buffer.get_page(schema.name, int(page_id_text))
            slot = int(slot_text)
            if slot < len(page.records):
                record = page.records[slot]
                yield _qualify(schema.name, record)

    def _nested_loop_join(self, node: PlanNode) -> list[dict[str, Any]]:
        join = node.join
        if not join:
            raise ValueError("Join node missing join clause")
        left_rows = self._execute_node(node.children[0])
        right_rows = self._execute_node(node.children[1])
        result: list[dict[str, Any]] = []
        for left in left_rows:
            left_value = _resolve(left, join.left_column)
            for right in right_rows:
                if left_value == _resolve(right, join.right_column):
                    result.append(_merge_join_rows(left, right))
        return result

    def _hash_join(self, node: PlanNode) -> list[dict[str, Any]]:
        join = node.join
        if not join:
            raise ValueError("Join node missing join clause")
        left_rows = self._execute_node(node.children[0])
        right_rows = self._execute_node(node.children[1])
        buckets: dict[Any, list[dict[str, Any]]] = {}
        for right in right_rows:
            buckets.setdefault(_resolve(right, join.right_column), []).append(right)
        result: list[dict[str, Any]] = []
        for left in left_rows:
            for right in buckets.get(_resolve(left, join.left_column), []):
                result.append(_merge_join_rows(left, right))
        return result

    def _project(self, row: dict[str, Any], columns: list[str]) -> dict[str, Any]:
        if columns == ["*"]:
            keys = _visible_keys(row)
            return {key: row[key] for key in keys}
        projected: dict[str, Any] = {}
        for column in columns:
            projected[column] = _resolve(row, column)
        return projected


def _qualify(table: str, record: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"_table": table, "_rid": record["_rid"], "_joined": False}
    for key, value in record.items():
        if key.startswith("_"):
            continue
        row[key] = value
        row[f"{table}.{key}"] = value
    return row


def _merge_join_rows(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {"_joined": True}
    for source in (left, right):
        for key, value in source.items():
            if key.startswith("_"):
                continue
            if "." in key:
                merged[key] = value
    return merged


def _visible_keys(row: dict[str, Any]) -> list[str]:
    if row.get("_joined"):
        return [key for key in row if not key.startswith("_") and "." in key]
    return [key for key in row if not key.startswith("_") and "." not in key]


def _derive_columns(rows: list[dict[str, Any]], requested: list[str]) -> list[str]:
    if requested != ["*"]:
        return requested
    if not rows:
        return []
    return list(rows[0].keys())


def _eval_condition(row: dict[str, Any], condition: Condition) -> bool:
    left = _resolve(row, condition.column)
    right = condition.value
    if isinstance(right, str) and "." in right and right in row:
        right = _resolve(row, right)
    return OPS[condition.operator](left, right)


def _resolve(row: dict[str, Any], ref: str) -> Any:
    ref = ref.lower()
    if ref in row:
        return row[ref]
    matches = [value for key, value in row.items() if key.endswith(f".{ref}")]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise KeyError(f"Ambiguous column reference: {ref}")
    raise KeyError(f"Unknown column reference: {ref}")


def _sort_key(value: Any) -> tuple[int, Any]:
    return (1, "") if value is None else (0, value)
