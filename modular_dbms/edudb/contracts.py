"""Shared contract v1. B/C/D import these objects rather than legacy models."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

Value = int | float | str | bool | None
Row = list[Value]
TableId = ColumnId = IndexId = ViewId = str


class DBError(Exception):
    def __init__(self, code: str, message: str, position: int | None = None,
                 transaction_effect: str = "NONE") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.position = position
        self.transaction_effect = transaction_effect

    def to_dict(self) -> dict[str, Any]:
        return dict(code=self.code, message=self.message, position=self.position,
                    transaction_effect=self.transaction_effect)


@dataclass
class ColumnDef:
    id: str
    name: str
    type: str
    nullable: bool = True


@dataclass
class Constraint:
    kind: str
    columns: list[str]
    ref_table: str | None = None
    ref_columns: list[str] | None = None
    expression: Any = None


@dataclass
class IndexInfo:
    id: str
    name: str
    table_id: str
    column_ids: list[str]
    unique: bool = False
    root_page_id: int | None = None


@dataclass
class TableSchema:
    id: str
    name: str
    columns: list[ColumnDef]
    constraints: list[Constraint] = field(default_factory=list)
    indexes: list[IndexInfo] = field(default_factory=list)
    version: int = 1


@dataclass
class ResultColumn:
    slot_id: str
    name: str
    type: str
    nullable: bool


@dataclass
class ViewDef:
    id: str
    name: str
    owner_id: str
    query_ast: Any
    output_columns: list[ResultColumn]
    dependencies: list[str]
    security_mode: str = "INVOKER"


@dataclass(frozen=True, order=True)
class RID:
    page_id: int
    slot_id: int


@dataclass
class Record:
    rid: RID
    values: list[Value]


@dataclass
class ObjectInfo:
    id: str
    name: str
    kind: str
    owner_id: str


@dataclass
class TableStats:
    row_count: int
    page_count: int
    column_stats: dict[str, Any] | None = None
    updated_at: str | None = None


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


@dataclass
class Transaction:
    id: str
    session_id: str
    state: str = "ACTIVE"
    last_lsn: int | None = None


@dataclass
class Session:
    id: str
    user_id: str
    optimizer_enabled: bool = True
    active_tx_id: str | None = None


@dataclass
class ExecutionContext:
    query_id: str
    session_id: str
    user_id: str
    tx_id: str
    metrics: Metrics = field(default_factory=Metrics)
    memory_limit_bytes: int = 16 * 1024 * 1024
    outer_rows: list[Row] | None = None


@dataclass(frozen=True, order=True)
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
    before_image: Page | None
    after_image: Page | None


@dataclass
class LogRecord:
    tx_id: str
    kind: str
    lsn: int = 0
    prev_lsn: int | None = None
    change: PageChange | None = None
    undo_next_lsn: int | None = None
    checkpoint_data: Any = None


@dataclass(frozen=True)
class PageHandle:
    key: PageKey
    mode: str
    _snapshot: Page = field(repr=False)
    _token: str = field(repr=False)
    _active: bool = field(default=True, repr=False)

    @property
    def page(self) -> Page:
        from copy import deepcopy
        if not self._active:
            raise DBError("INVALID_HANDLE", "Page handle has been released")
        return deepcopy(self._snapshot)


@dataclass(frozen=True)
class PreparedChange:
    rid: RID | None
    changes: list[PageChange]
    handles: list[PageHandle]
    _token: str = field(repr=False)


@dataclass
class CatalogCommand:
    action: str
    owner_id: str
    object_id: str | None = None
    table: TableSchema | None = None
    view: ViewDef | None = None
    index: IndexInfo | None = None
    transaction: Transaction | None = None


@dataclass
class CompileOptions:
    optimizer_enabled: bool = True


@dataclass
class Expr:
    kind: str
    properties: dict[str, Any] = field(default_factory=dict)
    children: list[Expr] = field(default_factory=list)


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
class PrivilegeRequest:
    object_type: str
    object_id: str
    action: str


@dataclass
class CompiledRequest:
    kind: str
    required_privileges: list[PrivilegeRequest]
    catalog_version: int
    plan: PlanNode | None = None
    command: Any = None


@dataclass
class QueryResult:
    columns: list[ResultColumn]
    rows: list[Row]
    affected_rows: int = 0
    plan: PlanNode | None = None
    metrics: Metrics = field(default_factory=Metrics)
    message: str | None = None


@dataclass
class ComparisonResult:
    off: QueryResult
    on: QueryResult
    equivalent: bool
    settings: dict[str, Any]
    samples: list[Any]
    summary: dict[str, Any]


@dataclass
class AuditEvent:
    time_utc: str
    request_id: str
    action: str
    object_ids: list[str]
    success: bool
    user_id: str | None = None
    session_id: str | None = None
    sql_redacted: str | None = None
    error_code: str | None = None
    metrics: Metrics | None = None


class LogService(Protocol):
    def append_log(self, record: LogRecord) -> int: ...
    def ensure_log_durable(self, lsn: int) -> None: ...
    # Read-only integration extension: A verifies supplied LSNs before publication.
    def get_log_record(self, lsn: int) -> LogRecord: ...


class TransactionService(Protocol):
    def begin(self, session_id: str) -> Transaction: ...
    def get_transaction(self, tx_id: str, session_id: str) -> Transaction: ...
    def acquire_lock(self, tx_id: str, resource: str, mode: str, timeout_ms: int) -> None: ...
    def commit(self, tx_id: str) -> None: ...
    def rollback(self, tx_id: str) -> None: ...
    def has_active_transactions(self) -> bool: ...
