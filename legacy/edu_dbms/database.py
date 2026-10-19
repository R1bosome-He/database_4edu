from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from .audit import AuditLogger
from .buffer import BufferManager
from .catalog import Catalog
from .executor import QueryExecutor
from .executor import _eval_condition, _qualify
from .index import IndexManager
from .models import (
    BeginTransactionStatement,
    Column,
    CompareStatement,
    CommitTransactionStatement,
    CreateIndexStatement,
    CreateTableStatement,
    CreateUserStatement,
    DeleteStatement,
    ExplainStatement,
    GrantStatement,
    InsertStatement,
    QueryResult,
    RollbackTransactionStatement,
    SelectStatement,
    SetOptimizerStatement,
    Statement,
    TableSchema,
    UpdateStatement,
)
from .planner import QueryPlanner
from .security import SecurityManager
from .sqlparser import SQLParser
from .storage import MAX_RECORDS_PER_PAGE, StorageManager


class Database:
    """Facade that wires parser, catalog, storage, buffer, index, optimizer, and security."""

    def __init__(self, path: str | Path = ".edudb", buffer_pages: int = 16) -> None:
        self.root = Path(path)
        self.root.mkdir(parents=True, exist_ok=True)
        self.catalog = Catalog(self.root)
        self.storage = StorageManager(self.root)
        self.buffer = BufferManager(self.storage, capacity=buffer_pages)
        self.indexes = IndexManager(self.root, self.catalog)
        self.planner = QueryPlanner(self.catalog)
        self.executor = QueryExecutor(self.catalog, self.buffer, self.indexes, self.planner)
        self.security = SecurityManager(self.root)
        self.audit = AuditLogger(self.root)
        self.parser = SQLParser()
        self.optimizer_enabled = True
        self._primary_key_cache: dict[str, set[Any]] = {}
        self._transaction_backup: Path | None = None
        self._transaction_tempdir: Path | None = None

    @classmethod
    def reset(cls, path: str | Path, buffer_pages: int = 16) -> "Database":
        target = Path(path)
        if target.exists():
            shutil.rmtree(target)
        return cls(target, buffer_pages=buffer_pages)

    def login(self, username: str, password: str) -> bool:
        return self.security.authenticate(username, password)

    def execute(self, sql: str, user: str = "admin") -> QueryResult:
        start = time.perf_counter()
        success = False
        message = ""
        metrics: dict[str, Any] = {}
        try:
            statement = self.parser.parse(sql)
            result = self.execute_statement(statement, user=user)
            success = True
            message = result.message or result.kind
            metrics = result.metrics
            return result
        except Exception as exc:
            message = str(exc)
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            self.audit.log(
                user=user,
                sql=sql,
                success=success,
                message=message,
                elapsed_ms=elapsed_ms,
                metrics=metrics,
            )

    def execute_statement(self, statement: Statement, user: str = "admin") -> QueryResult:
        if isinstance(statement, BeginTransactionStatement):
            return self._begin_transaction()
        if isinstance(statement, CommitTransactionStatement):
            return self._commit_transaction()
        if isinstance(statement, RollbackTransactionStatement):
            return self._rollback_transaction()
        if isinstance(statement, SetOptimizerStatement):
            self.optimizer_enabled = statement.enabled
            return QueryResult(
                kind="message",
                message=f"Optimizer is now {'ON' if statement.enabled else 'OFF'}",
            )
        if isinstance(statement, CreateTableStatement):
            return self._create_table(statement, user)
        if isinstance(statement, InsertStatement):
            return self._insert(statement, user)
        if isinstance(statement, UpdateStatement):
            return self._update(statement, user)
        if isinstance(statement, DeleteStatement):
            return self._delete(statement, user)
        if isinstance(statement, CreateIndexStatement):
            return self._create_index(statement, user)
        if isinstance(statement, SelectStatement):
            tables = [statement.table] + ([statement.join.table] if statement.join else [])
            self.security.require(user, "SELECT", tables)
            return self.executor.execute_select(statement, optimize=self.optimizer_enabled)
        if isinstance(statement, ExplainStatement):
            tables = [statement.query.table] + ([statement.query.join.table] if statement.query.join else [])
            self.security.require(user, "SELECT", tables)
            optimize = self.optimizer_enabled if statement.optimizer is None else statement.optimizer
            return QueryResult(
                kind="explain",
                plan=self.executor.explain(statement.query, optimize=optimize),
                message=f"Optimizer {'ON' if optimize else 'OFF'} plan",
            )
        if isinstance(statement, CompareStatement):
            tables = [statement.query.table] + ([statement.query.join.table] if statement.query.join else [])
            self.security.require(user, "SELECT", tables)
            off = self.executor.execute_select(statement.query, optimize=False)
            on = self.executor.execute_select(statement.query, optimize=True)
            return QueryResult(
                kind="compare",
                message="Optimizer OFF vs ON",
                comparisons=[off, on],
            )
        if isinstance(statement, CreateUserStatement):
            self.security.require_admin(user)
            self.security.create_user(statement.username, statement.password)
            return QueryResult(kind="message", message=f"User created: {statement.username}")
        if isinstance(statement, GrantStatement):
            self.security.require_admin(user)
            self.catalog.table(statement.table)
            self.security.grant(statement.username, statement.table, statement.action)
            return QueryResult(
                kind="message",
                message=f"Granted {statement.action} on {statement.table} to {statement.username}",
            )
        raise ValueError(f"Unsupported statement: {statement!r}")

    def scan_records(self, table: str) -> Iterable[dict[str, Any]]:
        schema = self.catalog.table(table)
        for page_id in range(schema.page_count):
            page = self.buffer.get_page(schema.name, page_id)
            yield from page.records

    def _create_table(self, statement: CreateTableStatement, user: str) -> QueryResult:
        self.security.require_admin(user)
        schema = self.catalog.create_table(statement.table, statement.columns, statement.primary_key)
        self.storage.create_table(schema)
        return QueryResult(kind="message", message=f"Table created: {schema.name}")

    def _insert(self, statement: InsertStatement, user: str) -> QueryResult:
        schema = self.catalog.table(statement.table)
        self.security.require(user, "INSERT", [schema.name])
        values = self._coerce_insert_values(schema, statement)
        if schema.primary_key:
            self._check_primary_key(schema, values[schema.primary_key])

        if schema.page_count == 0:
            page_id = 0
            schema.page_count = 1
        else:
            page_id = schema.page_count - 1

        page = self.buffer.get_page(schema.name, page_id)
        if len(page.records) >= MAX_RECORDS_PER_PAGE:
            page_id = schema.page_count
            schema.page_count += 1
            page = self.buffer.get_page(schema.name, page_id)

        rid = f"{page_id}:{len(page.records)}"
        record = {"_rid": rid, **values}
        page.records.append(record)
        schema.row_count += 1
        self.buffer.mark_dirty(page)
        self.catalog.update_table(schema)
        self.indexes.insert_record(schema, record)
        self.buffer.flush_all()
        return QueryResult(kind="message", message=f"1 row inserted into {schema.name}")

    def _update(self, statement: UpdateStatement, user: str) -> QueryResult:
        schema = self.catalog.table(statement.table)
        self.security.require(user, "UPDATE", [schema.name])
        assignments = self._coerce_update_values(schema, statement.assignments)
        existing_primary_keys: set[Any] = set()
        if schema.primary_key and schema.primary_key in assignments:
            existing_primary_keys = {
                record.get(schema.primary_key) for record in self.scan_records(schema.name)
            }

        updated = 0
        self.buffer.reset_metrics()
        for page_id in range(schema.page_count):
            page = self.buffer.get_page(schema.name, page_id)
            changed = False
            for record in page.records:
                if not all(_eval_condition(_qualify(schema.name, record), c) for c in statement.where):
                    continue
                if schema.primary_key and schema.primary_key in assignments:
                    old_value = record.get(schema.primary_key)
                    new_value = assignments[schema.primary_key]
                    existing_primary_keys.discard(old_value)
                    if new_value in existing_primary_keys:
                        raise ValueError(f"Duplicate primary key {schema.primary_key}={new_value!r}")
                    existing_primary_keys.add(new_value)
                record.update(assignments)
                updated += 1
                changed = True
            if changed:
                self.buffer.mark_dirty(page)

        if updated:
            self.buffer.flush_all()
            self.indexes.rebuild_table(schema.name, self.scan_records)
            self._primary_key_cache.pop(schema.name, None)
        return QueryResult(
            kind="message",
            message=f"{updated} row{'s' if updated != 1 else ''} updated in {schema.name}",
            metrics=self.buffer.snapshot_metrics(),
        )

    def _delete(self, statement: DeleteStatement, user: str) -> QueryResult:
        schema = self.catalog.table(statement.table)
        self.security.require(user, "DELETE", [schema.name])
        deleted = 0
        self.buffer.reset_metrics()
        for page_id in range(schema.page_count):
            page = self.buffer.get_page(schema.name, page_id)
            kept: list[dict[str, Any]] = []
            for record in page.records:
                if all(_eval_condition(_qualify(schema.name, record), c) for c in statement.where):
                    deleted += 1
                else:
                    kept.append(record)
            if len(kept) != len(page.records):
                for slot, record in enumerate(kept):
                    record["_rid"] = f"{page_id}:{slot}"
                page.records = kept
                self.buffer.mark_dirty(page)

        if deleted:
            schema.row_count -= deleted
            self.catalog.update_table(schema)
            self.buffer.flush_all()
            self.indexes.rebuild_table(schema.name, self.scan_records)
            self._primary_key_cache.pop(schema.name, None)
        return QueryResult(
            kind="message",
            message=f"{deleted} row{'s' if deleted != 1 else ''} deleted from {schema.name}",
            metrics=self.buffer.snapshot_metrics(),
        )

    def _create_index(self, statement: CreateIndexStatement, user: str) -> QueryResult:
        self.security.require_admin(user)
        schema = self.catalog.table(statement.table)
        schema.column(statement.column)
        self.buffer.reset_metrics()
        self.indexes.create_index(schema.name, statement.column, statement.name, self.scan_records)
        self.buffer.flush_all()
        return QueryResult(
            kind="message",
            message=f"B+ tree index created: {statement.name} on {schema.name}({statement.column})",
            metrics=self.buffer.snapshot_metrics(),
        )

    def _coerce_insert_values(
        self, schema: TableSchema, statement: InsertStatement
    ) -> dict[str, Any]:
        columns = statement.columns or schema.column_names
        if len(columns) != len(statement.values):
            raise ValueError("Column count does not match value count")
        values = {column.lower(): value for column, value in zip(columns, statement.values)}
        missing = [column for column in schema.column_names if column not in values]
        if missing:
            raise ValueError(f"Missing columns: {', '.join(missing)}")
        extra = [column for column in values if column not in schema.column_names]
        if extra:
            raise ValueError(f"Unknown columns: {', '.join(extra)}")
        return {column.name: _coerce_value(values[column.name], column) for column in schema.columns}

    def _coerce_update_values(self, schema: TableSchema, assignments: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for column_name, value in assignments.items():
            column = schema.column(column_name)
            values[column.name] = _coerce_value(value, column)
        return values

    def _check_primary_key(self, schema: TableSchema, value: Any) -> None:
        if not schema.primary_key:
            return
        cache = self._primary_key_cache.get(schema.name)
        if cache is None:
            cache = {record.get(schema.primary_key) for record in self.scan_records(schema.name)}
            self._primary_key_cache[schema.name] = cache
        if value in cache:
            raise ValueError(f"Duplicate primary key {schema.primary_key}={value!r}")
        cache.add(value)

    def _begin_transaction(self) -> QueryResult:
        if self._transaction_backup:
            raise RuntimeError("Transaction already active")
        self.buffer.flush_all()
        tempdir = Path(tempfile.mkdtemp(prefix="edudb_tx_"))
        backup = tempdir / "backup"
        shutil.copytree(self.root, backup)
        self._transaction_tempdir = tempdir
        self._transaction_backup = backup
        return QueryResult(kind="message", message="Transaction started")

    def _commit_transaction(self) -> QueryResult:
        if not self._transaction_backup or not self._transaction_tempdir:
            raise RuntimeError("No active transaction")
        shutil.rmtree(self._transaction_tempdir, ignore_errors=True)
        self._transaction_backup = None
        self._transaction_tempdir = None
        return QueryResult(kind="message", message="Transaction committed")

    def _rollback_transaction(self) -> QueryResult:
        if not self._transaction_backup or not self._transaction_tempdir:
            raise RuntimeError("No active transaction")
        self.buffer.clear()
        backup = self._transaction_backup
        tempdir = self._transaction_tempdir
        if self.root.exists():
            shutil.rmtree(self.root)
        shutil.copytree(backup, self.root)
        self._transaction_backup = None
        self._transaction_tempdir = None
        shutil.rmtree(tempdir, ignore_errors=True)
        self._reopen()
        return QueryResult(kind="message", message="Transaction rolled back")

    def _reopen(self) -> None:
        self.catalog = Catalog(self.root)
        self.storage = StorageManager(self.root)
        self.buffer = BufferManager(self.storage, capacity=self.buffer.capacity)
        self.indexes = IndexManager(self.root, self.catalog)
        self.planner = QueryPlanner(self.catalog)
        self.executor = QueryExecutor(self.catalog, self.buffer, self.indexes, self.planner)
        self.security = SecurityManager(self.root)
        self.audit = AuditLogger(self.root)
        self._primary_key_cache.clear()


def _coerce_value(value: Any, column: Column) -> Any:
    if value is None:
        return None
    if column.type == "INT":
        if isinstance(value, bool):
            raise ValueError(f"Invalid INT value for {column.name}: {value!r}")
        return int(value)
    if column.type == "FLOAT":
        return float(value)
    if column.type == "TEXT":
        return str(value)
    if column.type == "BOOL":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise ValueError(f"Invalid BOOL value for {column.name}: {value!r}")
    raise ValueError(f"Unsupported type: {column.type}")
