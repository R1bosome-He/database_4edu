from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .catalog import Catalog
from .models import Condition, JoinClause, OrderBy, SelectStatement


@dataclass(slots=True)
class PlanNode:
    op: str
    table: str | None = None
    columns: list[str] = field(default_factory=list)
    condition: Condition | None = None
    conditions: list[Condition] = field(default_factory=list)
    index_column: str | None = None
    join: JoinClause | None = None
    order_by: list[OrderBy] = field(default_factory=list)
    children: list["PlanNode"] = field(default_factory=list)
    detail: str = ""
    estimated_rows: int | None = None

    def format(self, indent: int = 0) -> str:
        prefix = "  " * indent
        head = f"{prefix}{self.op}"
        attrs: list[str] = []
        if self.table:
            attrs.append(f"table={self.table}")
        if self.index_column:
            attrs.append(f"index={self.index_column}")
        if self.condition:
            attrs.append(f"condition={self.condition.describe()}")
        if self.conditions:
            attrs.append("conditions=" + " AND ".join(c.describe() for c in self.conditions))
        if self.join:
            attrs.append(f"on={self.join.left_column} = {self.join.right_column}")
        if self.order_by:
            attrs.append("order_by=" + ", ".join(item.describe() for item in self.order_by))
        if self.columns:
            attrs.append("columns=" + ", ".join(self.columns))
        if self.estimated_rows is not None:
            attrs.append(f"est_rows={self.estimated_rows}")
        if self.detail:
            attrs.append(self.detail)
        if attrs:
            head += " [" + "; ".join(attrs) + "]"
        child_text = [child.format(indent + 1) for child in self.children]
        return "\n".join([head, *child_text])


class QueryPlanner:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def build(self, query: SelectStatement, optimize: bool = True) -> PlanNode:
        left_conditions, right_conditions, residual_conditions = self._partition_conditions(query)
        if optimize:
            left = self._optimized_scan(query.table, left_conditions)
        else:
            left = self._seq_scan(query.table)

        current = left
        if query.join:
            right = (
                self._optimized_scan(query.join.table, right_conditions)
                if optimize
                else self._seq_scan(query.join.table)
            )
            current = PlanNode(
                "HashJoin" if optimize else "NestedLoopJoin",
                join=query.join,
                children=[left, right],
                estimated_rows=max(1, self._estimate_rows(query.table) // 4),
                detail="optimized equi-join" if optimize else "optimizer disabled",
            )

        filters = residual_conditions if optimize else query.where
        if filters:
            current = PlanNode(
                "Filter",
                conditions=filters,
                children=[current],
                estimated_rows=max(1, (current.estimated_rows or 1) // 3),
            )

        if query.order_by:
            current = PlanNode(
                "Sort",
                order_by=query.order_by,
                children=[current],
                estimated_rows=current.estimated_rows,
                detail="ORDER BY",
            )

        return PlanNode(
            "Projection",
            columns=query.columns,
            children=[current],
            estimated_rows=current.estimated_rows,
        )

    def _optimized_scan(self, table: str, conditions: list[Condition]) -> PlanNode:
        schema = self.catalog.table(table)
        chosen: Condition | None = None
        for condition in conditions:
            column = _unqualified(condition.column)
            if condition.operator in {"=", "<", "<=", ">", ">="} and schema.has_index(column):
                chosen = condition
                break

        if chosen:
            scan = PlanNode(
                "IndexScan",
                table=schema.name,
                condition=chosen,
                index_column=_unqualified(chosen.column),
                estimated_rows=max(1, schema.row_count // 20),
                detail="selected by optimizer",
            )
            remaining = [condition for condition in conditions if condition is not chosen]
        else:
            scan = self._seq_scan(schema.name)
            remaining = conditions

        if remaining:
            return PlanNode(
                "Filter",
                conditions=remaining,
                children=[scan],
                estimated_rows=max(1, (scan.estimated_rows or 1) // 3),
                detail="predicate pushdown",
            )
        return scan

    def _seq_scan(self, table: str) -> PlanNode:
        schema = self.catalog.table(table)
        return PlanNode("SeqScan", table=schema.name, estimated_rows=max(1, schema.row_count))

    def _partition_conditions(
        self, query: SelectStatement
    ) -> tuple[list[Condition], list[Condition], list[Condition]]:
        if not query.join:
            return list(query.where), [], []

        left_table = query.table.lower()
        right_table = query.join.table.lower()
        left_columns = set(self.catalog.table(left_table).column_names)
        right_columns = set(self.catalog.table(right_table).column_names)
        left: list[Condition] = []
        right: list[Condition] = []
        residual: list[Condition] = []

        for condition in query.where:
            table_name, column = _split_ref(condition.column)
            if table_name == left_table or (table_name is None and column in left_columns - right_columns):
                left.append(condition)
            elif table_name == right_table or (table_name is None and column in right_columns - left_columns):
                right.append(condition)
            else:
                residual.append(condition)
        return left, right, residual

    def _estimate_rows(self, table: str) -> int:
        return max(1, self.catalog.table(table).row_count)


def _split_ref(ref: str) -> tuple[str | None, str]:
    lowered = ref.lower()
    if "." not in lowered:
        return None, lowered
    table, column = lowered.split(".", 1)
    return table, column


def _unqualified(ref: str) -> str:
    return _split_ref(ref)[1]
