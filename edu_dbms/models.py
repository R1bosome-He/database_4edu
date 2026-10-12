from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SUPPORTED_TYPES = {"INT", "FLOAT", "TEXT", "BOOL"}


@dataclass(slots=True)
class Column:
    name: str
    type: str

    def __post_init__(self) -> None:
        self.name = self.name.lower()
        self.type = self.type.upper()
        if self.type not in SUPPORTED_TYPES:
            raise ValueError(f"Unsupported column type: {self.type}")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "type": self.type}

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "Column":
        return cls(data["name"], data["type"])


@dataclass(slots=True)
class IndexMeta:
    name: str
    column: str

    def __post_init__(self) -> None:
        self.name = self.name.lower()
        self.column = self.column.lower()

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "column": self.column}

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "IndexMeta":
        return cls(data["name"], data["column"])


@dataclass(slots=True)
class TableSchema:
    name: str
    columns: list[Column]
    primary_key: str | None = None
    page_count: int = 0
    row_count: int = 0
    indexes: list[IndexMeta] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.name = self.name.lower()
        self.primary_key = self.primary_key.lower() if self.primary_key else None
        seen: set[str] = set()
        for column in self.columns:
            if column.name in seen:
                raise ValueError(f"Duplicate column: {column.name}")
            seen.add(column.name)
        if self.primary_key and self.primary_key not in seen:
            raise ValueError(f"Primary key does not exist: {self.primary_key}")

    @property
    def column_names(self) -> list[str]:
        return [column.name for column in self.columns]

    def column(self, name: str) -> Column:
        lowered = name.lower()
        for column in self.columns:
            if column.name == lowered:
                return column
        raise KeyError(f"Unknown column {name} in table {self.name}")

    def has_index(self, column: str) -> bool:
        lowered = column.lower()
        return any(index.column == lowered for index in self.indexes)

    def index_for(self, column: str) -> IndexMeta | None:
        lowered = column.lower()
        for index in self.indexes:
            if index.column == lowered:
                return index
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "columns": [column.to_dict() for column in self.columns],
            "primary_key": self.primary_key,
            "page_count": self.page_count,
            "row_count": self.row_count,
            "indexes": [index.to_dict() for index in self.indexes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TableSchema":
        return cls(
            name=data["name"],
            columns=[Column.from_dict(item) for item in data["columns"]],
            primary_key=data.get("primary_key"),
            page_count=int(data.get("page_count", 0)),
            row_count=int(data.get("row_count", 0)),
            indexes=[IndexMeta.from_dict(item) for item in data.get("indexes", [])],
        )


@dataclass(slots=True)
class Page:
    table: str
    page_id: int
    records: list[dict[str, Any]]

    @property
    def key(self) -> tuple[str, int]:
        return (self.table, self.page_id)


@dataclass(slots=True)
class BufferMetrics:
    cache_hits: int = 0
    cache_misses: int = 0
    disk_reads: int = 0
    disk_writes: int = 0
    evictions: int = 0
    pages_touched: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "disk_reads": self.disk_reads,
            "disk_writes": self.disk_writes,
            "evictions": self.evictions,
            "pages_touched": self.pages_touched,
        }


@dataclass(slots=True)
class Condition:
    column: str
    operator: str
    value: Any

    def __post_init__(self) -> None:
        self.column = self.column.lower()
        if self.operator == "<>":
            self.operator = "!="

    def describe(self) -> str:
        return f"{self.column} {self.operator} {self.value!r}"


@dataclass(slots=True)
class JoinClause:
    table: str
    left_column: str
    right_column: str

    def __post_init__(self) -> None:
        self.table = self.table.lower()
        self.left_column = self.left_column.lower()
        self.right_column = self.right_column.lower()


@dataclass(slots=True)
class OrderBy:
    column: str
    descending: bool = False

    def __post_init__(self) -> None:
        self.column = self.column.lower()

    def describe(self) -> str:
        return f"{self.column} {'DESC' if self.descending else 'ASC'}"


@dataclass(slots=True)
class CreateTableStatement:
    table: str
    columns: list[Column]
    primary_key: str | None


@dataclass(slots=True)
class CreateIndexStatement:
    name: str
    table: str
    column: str


@dataclass(slots=True)
class InsertStatement:
    table: str
    columns: list[str] | None
    values: list[Any]


@dataclass(slots=True)
class UpdateStatement:
    table: str
    assignments: dict[str, Any]
    where: list[Condition] = field(default_factory=list)


@dataclass(slots=True)
class DeleteStatement:
    table: str
    where: list[Condition] = field(default_factory=list)


@dataclass(slots=True)
class SelectStatement:
    columns: list[str]
    table: str
    where: list[Condition] = field(default_factory=list)
    join: JoinClause | None = None
    order_by: list[OrderBy] = field(default_factory=list)


@dataclass(slots=True)
class ExplainStatement:
    query: SelectStatement
    optimizer: bool | None = None


@dataclass(slots=True)
class CompareStatement:
    query: SelectStatement


@dataclass(slots=True)
class SetOptimizerStatement:
    enabled: bool


@dataclass(slots=True)
class BeginTransactionStatement:
    pass


@dataclass(slots=True)
class CommitTransactionStatement:
    pass


@dataclass(slots=True)
class RollbackTransactionStatement:
    pass


@dataclass(slots=True)
class CreateUserStatement:
    username: str
    password: str


@dataclass(slots=True)
class GrantStatement:
    action: str
    table: str
    username: str


Statement = (
    CreateTableStatement
    | CreateIndexStatement
    | InsertStatement
    | UpdateStatement
    | DeleteStatement
    | SelectStatement
    | ExplainStatement
    | CompareStatement
    | SetOptimizerStatement
    | BeginTransactionStatement
    | CommitTransactionStatement
    | RollbackTransactionStatement
    | CreateUserStatement
    | GrantStatement
)


@dataclass(slots=True)
class QueryResult:
    kind: str
    message: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    plan: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    comparisons: list["QueryResult"] = field(default_factory=list)
