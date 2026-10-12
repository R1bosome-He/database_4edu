from __future__ import annotations

import re
from typing import Any

from .models import (
    BeginTransactionStatement,
    Column,
    CompareStatement,
    CommitTransactionStatement,
    Condition,
    CreateIndexStatement,
    CreateTableStatement,
    CreateUserStatement,
    DeleteStatement,
    ExplainStatement,
    GrantStatement,
    InsertStatement,
    JoinClause,
    OrderBy,
    RollbackTransactionStatement,
    SelectStatement,
    SetOptimizerStatement,
    Statement,
    UpdateStatement,
)


class SQLParser:
    """Small SQL parser for the supported coursework subset."""

    def parse(self, sql: str) -> Statement:
        sql = _strip_sql(sql)
        if not sql:
            raise ValueError("Empty SQL")

        if re.fullmatch(r"BEGIN(?:\s+TRANSACTION)?", sql, re.I):
            return BeginTransactionStatement()
        if re.fullmatch(r"COMMIT", sql, re.I):
            return CommitTransactionStatement()
        if re.fullmatch(r"ROLLBACK", sql, re.I):
            return RollbackTransactionStatement()

        match = re.fullmatch(r"SET\s+OPTIMIZER\s+(ON|OFF)", sql, re.I)
        if match:
            return SetOptimizerStatement(match.group(1).upper() == "ON")

        match = re.match(r"EXPLAIN(?:\s+OPTIMIZER\s+(ON|OFF))?\s+(.+)$", sql, re.I | re.S)
        if match:
            optimizer = None
            if match.group(1):
                optimizer = match.group(1).upper() == "ON"
            query = self._parse_select(match.group(2))
            return ExplainStatement(query, optimizer)

        match = re.match(r"COMPARE\s+(.+)$", sql, re.I | re.S)
        if match:
            return CompareStatement(self._parse_select(match.group(1)))

        if re.match(r"CREATE\s+TABLE\b", sql, re.I):
            return self._parse_create_table(sql)
        if re.match(r"CREATE\s+INDEX\b", sql, re.I):
            return self._parse_create_index(sql)
        if re.match(r"INSERT\s+INTO\b", sql, re.I):
            return self._parse_insert(sql)
        if re.match(r"UPDATE\b", sql, re.I):
            return self._parse_update(sql)
        if re.match(r"DELETE\s+FROM\b", sql, re.I):
            return self._parse_delete(sql)
        if re.match(r"SELECT\b", sql, re.I):
            return self._parse_select(sql)
        if re.match(r"CREATE\s+USER\b", sql, re.I):
            return self._parse_create_user(sql)
        if re.match(r"GRANT\b", sql, re.I):
            return self._parse_grant(sql)

        raise ValueError(f"Unsupported SQL statement: {sql}")

    def _parse_create_table(self, sql: str) -> CreateTableStatement:
        match = re.fullmatch(r"CREATE\s+TABLE\s+(\w+)\s*\((.+)\)", sql, re.I | re.S)
        if not match:
            raise ValueError("CREATE TABLE syntax: CREATE TABLE name (col TYPE, ...)")
        table = match.group(1).lower()
        parts = _split_csv(match.group(2))
        columns: list[Column] = []
        primary_key: str | None = None
        for part in parts:
            pk = re.fullmatch(r"PRIMARY\s+KEY\s*\((\w+)\)", part, re.I)
            if pk:
                primary_key = pk.group(1).lower()
                continue
            column_match = re.fullmatch(r"(\w+)\s+(INT|FLOAT|TEXT|BOOL)", part, re.I)
            if not column_match:
                raise ValueError(f"Invalid column definition: {part}")
            columns.append(Column(column_match.group(1), column_match.group(2)))
        if not columns:
            raise ValueError("CREATE TABLE requires at least one column")
        return CreateTableStatement(table, columns, primary_key)

    def _parse_create_index(self, sql: str) -> CreateIndexStatement:
        match = re.fullmatch(
            r"CREATE\s+INDEX\s+(\w+)\s+ON\s+(\w+)\s*\((\w+)\)",
            sql,
            re.I,
        )
        if not match:
            raise ValueError("CREATE INDEX syntax: CREATE INDEX idx ON table (column)")
        return CreateIndexStatement(
            name=match.group(1).lower(),
            table=match.group(2).lower(),
            column=match.group(3).lower(),
        )

    def _parse_insert(self, sql: str) -> InsertStatement:
        match = re.fullmatch(
            r"INSERT\s+INTO\s+(\w+)(?:\s*\((.*?)\))?\s+VALUES\s*\((.+)\)",
            sql,
            re.I | re.S,
        )
        if not match:
            raise ValueError("INSERT syntax: INSERT INTO table (cols) VALUES (values)")
        columns = None
        if match.group(2):
            columns = [item.strip().lower() for item in _split_csv(match.group(2))]
        values = [_parse_literal(item) for item in _split_csv(match.group(3))]
        return InsertStatement(match.group(1).lower(), columns, values)

    def _parse_update(self, sql: str) -> UpdateStatement:
        match = re.fullmatch(r"UPDATE\s+(\w+)\s+SET\s+(.+)", sql, re.I | re.S)
        if not match:
            raise ValueError("UPDATE syntax: UPDATE table SET col = value [WHERE ...]")
        table = match.group(1).lower()
        set_part, where_part = _split_keyword(match.group(2).strip(), "WHERE")
        assignments: dict[str, Any] = {}
        for item in _split_csv(set_part):
            assign = re.fullmatch(r"(\w+)\s*=\s*(.+)", item, re.I | re.S)
            if not assign:
                raise ValueError(f"Invalid assignment: {item}")
            assignments[assign.group(1).lower()] = _parse_literal(assign.group(2))
        if not assignments:
            raise ValueError("UPDATE requires at least one assignment")
        conditions = self._parse_where(where_part) if where_part else []
        return UpdateStatement(table, assignments, conditions)

    def _parse_delete(self, sql: str) -> DeleteStatement:
        match = re.fullmatch(r"DELETE\s+FROM\s+(\w+)(?:\s+WHERE\s+(.+))?", sql, re.I | re.S)
        if not match:
            raise ValueError("DELETE syntax: DELETE FROM table [WHERE ...]")
        conditions = self._parse_where(match.group(2)) if match.group(2) else []
        return DeleteStatement(match.group(1).lower(), conditions)

    def _parse_select(self, sql: str) -> SelectStatement:
        sql = _strip_sql(sql)
        match = re.fullmatch(r"SELECT\s+(.+?)\s+FROM\s+(.+)", sql, re.I | re.S)
        if not match:
            raise ValueError("SELECT syntax: SELECT cols FROM table [JOIN ...] [WHERE ...] [ORDER BY ...]")
        columns = [item.strip().lower() for item in _split_csv(match.group(1))]
        from_tail = match.group(2).strip()
        from_and_where, order_part = _split_keyword(from_tail, "ORDER BY")
        from_part, where_part = _split_keyword(from_and_where, "WHERE")
        table, join = self._parse_from(from_part.strip())
        conditions = self._parse_where(where_part) if where_part else []
        order_by = self._parse_order_by(order_part) if order_part else []
        return SelectStatement(columns, table, conditions, join, order_by)

    def _parse_from(self, text: str) -> tuple[str, JoinClause | None]:
        join_match = re.fullmatch(
            r"(\w+)\s+JOIN\s+(\w+)\s+ON\s+([\w.]+)\s*=\s*([\w.]+)",
            text,
            re.I,
        )
        if join_match:
            return (
                join_match.group(1).lower(),
                JoinClause(
                    table=join_match.group(2),
                    left_column=join_match.group(3),
                    right_column=join_match.group(4),
                ),
            )
        table_match = re.fullmatch(r"(\w+)", text, re.I)
        if not table_match:
            raise ValueError(f"Invalid FROM clause: {text}")
        return table_match.group(1).lower(), None

    def _parse_where(self, text: str) -> list[Condition]:
        parts = re.split(r"\s+AND\s+", text.strip(), flags=re.I)
        conditions: list[Condition] = []
        for part in parts:
            match = re.fullmatch(r"([\w.]+)\s*(=|!=|<>|<=|>=|<|>)\s*(.+)", part.strip(), re.I)
            if not match:
                raise ValueError(f"Invalid WHERE condition: {part}")
            conditions.append(
                Condition(
                    column=match.group(1),
                    operator=match.group(2),
                    value=_parse_literal(match.group(3).strip()),
                )
            )
        return conditions

    def _parse_order_by(self, text: str) -> list[OrderBy]:
        order_items: list[OrderBy] = []
        for part in _split_csv(text):
            match = re.fullmatch(r"([\w.]+)(?:\s+(ASC|DESC))?", part.strip(), re.I)
            if not match:
                raise ValueError(f"Invalid ORDER BY item: {part}")
            order_items.append(OrderBy(match.group(1), (match.group(2) or "ASC").upper() == "DESC"))
        return order_items

    def _parse_create_user(self, sql: str) -> CreateUserStatement:
        match = re.fullmatch(
            r"CREATE\s+USER\s+(\w+)\s+PASSWORD\s+(.+)",
            sql,
            re.I | re.S,
        )
        if not match:
            raise ValueError("CREATE USER syntax: CREATE USER username PASSWORD 'password'")
        return CreateUserStatement(match.group(1).lower(), str(_parse_literal(match.group(2))))

    def _parse_grant(self, sql: str) -> GrantStatement:
        match = re.fullmatch(
            r"GRANT\s+(SELECT|INSERT|UPDATE|DELETE|ALL)\s+ON\s+(\w+)\s+TO\s+(\w+)",
            sql,
            re.I,
        )
        if not match:
            raise ValueError("GRANT syntax: GRANT SELECT|INSERT|UPDATE|DELETE|ALL ON table TO user")
        return GrantStatement(match.group(1).upper(), match.group(2).lower(), match.group(3).lower())


def _strip_sql(sql: str) -> str:
    return sql.strip().rstrip(";").strip()


def _split_keyword(text: str, keyword: str) -> tuple[str, str | None]:
    pattern = re.compile(rf"\s+{keyword}\s+", re.I)
    match = pattern.search(text)
    if not match:
        return text, None
    return text[: match.start()], text[match.end() :]


def _split_csv(text: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    depth = 0
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            current.append(char)
            if char == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    current.append(text[i + 1])
                    i += 1
                else:
                    quote = None
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        i += 1
    if current:
        items.append("".join(current).strip())
    return items


def _parse_literal(value: str) -> Any:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        inner = value[1:-1]
        return inner.replace(value[0] * 2, value[0])
    if re.fullmatch(r"NULL", value, re.I):
        return None
    if re.fullmatch(r"TRUE", value, re.I):
        return True
    if re.fullmatch(r"FALSE", value, re.I):
        return False
    if re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    if re.fullmatch(r"[+-]?\d+\.\d+", value):
        return float(value)
    return value.lower()
