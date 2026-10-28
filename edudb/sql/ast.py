"""Parsed SQL syntax tree for module B.

These are *unbound* nodes: identifiers are plain strings and no catalog lookup
has happened yet. Binding/planning (planner.py) turns them into C's c_api plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# --- expressions ---------------------------------------------------------

class Node:
    """Base marker so isinstance checks read clearly."""


@dataclass
class Literal(Node):
    value: object          # int | float | str | bool | None
    pos: int = 0


@dataclass
class ColumnName(Node):
    table: str | None
    name: str
    pos: int = 0


@dataclass
class Star(Node):
    table: str | None = None
    pos: int = 0


@dataclass
class BinaryOp(Node):
    op: str                # = <> < <= > >= + - * / % AND OR
    left: Node
    right: Node
    pos: int = 0


@dataclass
class UnaryOp(Node):
    op: str                # NOT + -
    operand: Node
    pos: int = 0


@dataclass
class IsNull(Node):
    operand: Node
    negated: bool = False
    pos: int = 0


@dataclass
class Between(Node):
    operand: Node
    low: Node
    high: Node
    negated: bool = False
    pos: int = 0


@dataclass
class InList(Node):
    operand: Node
    items: list[Node]
    negated: bool = False
    pos: int = 0


@dataclass
class InSubquery(Node):
    operand: Node
    subquery: "SelectStmt"
    negated: bool = False
    pos: int = 0


@dataclass
class ExistsExpr(Node):
    subquery: "SelectStmt"
    negated: bool = False
    pos: int = 0


@dataclass
class ScalarSubquery(Node):
    subquery: "SelectStmt"
    pos: int = 0


@dataclass
class FuncCall(Node):
    name: str              # lower-case function name
    args: list[Node]
    distinct: bool = False
    star: bool = False     # COUNT(*)
    pos: int = 0


# --- FROM / query --------------------------------------------------------

@dataclass
class TableRef(Node):
    name: str
    alias: str | None = None
    pos: int = 0


@dataclass
class JoinRef(Node):
    left: Node
    right: Node
    join_type: str         # INNER | LEFT | RIGHT | FULL | CROSS
    on: Node | None = None
    pos: int = 0


@dataclass
class SelectItem(Node):
    expr: Node             # or Star
    alias: str | None = None


@dataclass
class OrderItem(Node):
    expr: Node
    descending: bool = False
    nulls_first: bool | None = None   # None => SQL default (NULLS LAST here)


@dataclass
class SelectStmt(Node):
    select_list: list[SelectItem]
    from_: Node | None = None
    where: Node | None = None
    group_by: list[Node] = field(default_factory=list)
    having: Node | None = None
    order_by: list[OrderItem] = field(default_factory=list)
    distinct: bool = False
    limit: int | None = None
    offset: int = 0


# --- DML / DDL / control -------------------------------------------------

@dataclass
class InsertStmt(Node):
    table: str
    columns: list[str] | None
    rows: list[list[Node]]


@dataclass
class UpdateStmt(Node):
    table: str
    assignments: list[tuple[str, Node]]
    where: Node | None = None


@dataclass
class DeleteStmt(Node):
    table: str
    where: Node | None = None


@dataclass
class ColumnDef(Node):
    name: str
    type: str              # INT | FLOAT | TEXT | BOOL
    not_null: bool = False
    primary_key: bool = False


@dataclass
class TableConstraint(Node):
    kind: str              # PRIMARY_KEY | UNIQUE | CHECK | FOREIGN_KEY
    columns: list[str] = field(default_factory=list)
    ref_table: str | None = None
    ref_columns: list[str] | None = None
    check: Node | None = None


@dataclass
class CreateTableStmt(Node):
    name: str
    columns: list[ColumnDef]
    constraints: list[TableConstraint] = field(default_factory=list)


@dataclass
class CreateIndexStmt(Node):
    name: str
    table: str
    column: str
    unique: bool = False


@dataclass
class TxnStmt(Node):
    action: str            # BEGIN | COMMIT | ROLLBACK


@dataclass
class ExplainStmt(Node):
    inner: Node


@dataclass
class CreateViewStmt(Node):
    name: str
    query: SelectStmt
    query_sql: str


@dataclass
class DropViewStmt(Node):
    name: str
