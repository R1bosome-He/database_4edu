"""Shared C-facing contracts. No SQL parsing or web/authentication lives here."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .errors import DatabaseError

Value = int | float | str | bool | None
Row = list[Value]


class DBError(DatabaseError):
    def __init__(self, code: str, message: str, *, position: int | None = None,
                 transaction_effect: str = "NONE") -> None:
        super().__init__(message)
        self.code, self.message, self.position = code, message, position
        self.transaction_effect = transaction_effect

    def to_dict(self) -> dict[str, Any]:
        return dict(code=self.code, message=self.message, position=self.position,
                    transaction_effect=self.transaction_effect)


@dataclass(frozen=True)
class ResultColumn:
    slot_id: str
    name: str
    type: str
    nullable: bool = True


@dataclass
class Expr:
    kind: str
    value: Any = None
    slot_id: str | None = None
    operator: str | None = None
    args: list[Expr] = field(default_factory=list)
    level: int = 0
    plan: PlanNode | None = None
    distinct: bool = False


@dataclass
class PlanNode:
    node_id: str
    op: str
    children: list[PlanNode] = field(default_factory=list)
    output_columns: list[ResultColumn] = field(default_factory=list)
    expressions: list[Expr] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    estimated_rows: int | None = None
    estimated_cost: float | None = None
    applied_rules: list[str] = field(default_factory=list)


@dataclass
class OperatorMetrics:
    node_id: str
    input_rows: int = 0
    output_rows: int = 0
    rows_examined: int = 0
    join_comparisons: int = 0
    hash_build_rows: int = 0
    hash_probes: int = 0
    elapsed_ms: float = 0.0


@dataclass
class Metrics:
    planning_ms: float = 0.0
    execution_ms: float = 0.0
    rows_returned: int = 0
    data_page_reads: int = 0
    index_page_reads: int = 0
    page_writes: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    evictions: int = 0
    per_operator: dict[str, OperatorMetrics] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionContext:
    query_id: str
    session_id: str
    user_id: str
    tx_id: str
    metrics: Metrics = field(default_factory=Metrics)
    memory_limit_bytes: int = 64 * 1024 * 1024
    outer_rows: list[dict[str, Value]] = field(default_factory=list)
    lock_timeout_ms: int = 2000
    cancelled: bool = False
    # Shared by correlated children so materialization cannot bypass the budget.
    memory_used_bytes: int = 0


@dataclass
class QueryResult:
    columns: list[ResultColumn]
    rows: list[Row]
    affected_rows: int = 0
    plan: PlanNode | None = None
    metrics: Metrics = field(default_factory=Metrics)
    message: str | None = None
    teaching: dict | None = None


@dataclass(frozen=True)
class PageKey:
    file_id: str
    page_id: int


@dataclass
class Page:
    key: PageKey
    kind: str
    page_lsn: int
    payload: Any


@dataclass
class PageChange:
    key: PageKey
    before_image: dict[str, Any] | None
    after_image: dict[str, Any] | None


@dataclass
class Transaction:
    id: str
    session_id: str
    state: str = "ACTIVE"
    last_lsn: int | None = None


@dataclass(frozen=True)
class IndexSpec:
    name: str
    table_id: str
    column_ids: list[str]
    unique: bool = False


@dataclass
class Constraint:
    kind: str
    columns: list[str] = field(default_factory=list)
    ref_table: str | None = None
    ref_columns: list[str] = field(default_factory=list)
    expression: Expr | None = None


@dataclass
class IndexInfo:
    id: str
    name: str
    table_id: str
    column_ids: list[str]
    unique: bool
    root_page_id: int


@dataclass(frozen=True)
class RangeSpec:
    lower: Value = None
    upper: Value = None
    lower_inclusive: bool = True
    upper_inclusive: bool = True


class PageStore(Protocol):
    """A must implement these semantics; integration/ is a reference bridge."""
    def read_image(self, key: PageKey, metrics: Metrics | None = None) -> dict[str, Any] | None: ...
    def apply_image(self, key: PageKey, image: dict[str, Any] | None, lsn: int) -> None: ...
    def flush_all(self) -> None: ...
    def sync_files(self, file_ids: list[str] | None = None) -> None: ...
    def validate_image(self, image: dict[str, Any] | None) -> None: ...


class RecordAccess(Protocol):
    """Adapter for A's record API, including transaction-aware preparation."""
    def schema(self, table_id: str): ...
    def scan(self, table_id: str, ctx: ExecutionContext): ...
    def fetch(self, table_id: str, rid, ctx: ExecutionContext): ...
    def insert(self, table_id: str, values: dict[str, Value], ctx: ExecutionContext): ...
    def update(self, table_id: str, rid, values: dict[str, Value], ctx: ExecutionContext): ...
    def delete(self, table_id: str, rid, ctx: ExecutionContext): ...


def expression_from_dict(value: dict) -> Expr:
    data = dict(value)
    data["args"] = [expression_from_dict(a) for a in data.get("args", [])]
    if data.get("plan") is not None:
        data["plan"] = plan_from_dict(data["plan"])
    return Expr(**data)


def plan_from_dict(value: dict) -> PlanNode:
    def property_value(item):
        if isinstance(item, dict):
            if "kind" in item and item["kind"] in {"Literal", "ColumnRef", "Binary", "Unary", "IsNull", "Aggregate", "Subquery"}:
                return expression_from_dict(item)
            if {"slot_id", "name", "type", "nullable"} <= item.keys():
                return ResultColumn(**item)
            return {k: property_value(v) for k, v in item.items()}
        if isinstance(item, list):
            return [property_value(v) for v in item]
        return item
    data = dict(value)
    data["children"] = [plan_from_dict(n) for n in data.get("children", [])]
    data["output_columns"] = [ResultColumn(**c) for c in data.get("output_columns", [])]
    data["expressions"] = [expression_from_dict(e) for e in data.get("expressions", [])]
    data["properties"] = property_value(data.get("properties", {}))
    return PlanNode(**data)
