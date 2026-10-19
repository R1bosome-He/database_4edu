"""Executable reference composition, not B's parser or D's authenticated API."""
from __future__ import annotations

import os
import uuid
from dataclasses import asdict
from pathlib import Path

from ..c_api import Constraint, DBError, ExecutionContext, PageChange, PageKey, expression_from_dict
from ..execution import QueryExecutor
from ..index import IndexManager
from ..models import TableSchema, identifier
from ..transactions import TransactionManager, WAL
from .store import RecordAdapter, ReferencePageStore

CATALOG_KEY = PageKey("cm_catalog", 0)
CONSTRAINTS_KEY = PageKey("cm_constraints", 0)


class CatalogBridge:
    """A-compatible table/tuple API backed by a WAL-protected catalog page."""
    def __init__(self, pages) -> None:
        self.pages = pages

    def table(self, table_id):
        name = identifier(table_id)
        image = self.pages.read_image(CATALOG_KEY)
        payload = image["payload"] if image else {}
        if name not in payload:
            raise DBError("OBJECT_NOT_FOUND", name)
        return TableSchema.from_dict(payload[name])

    def tables(self):
        image = self.pages.read_image(CATALOG_KEY)
        return tuple(TableSchema.from_dict(v) for _, v in sorted((image["payload"] if image else {}).items()))

    def constraints(self, table_id):
        image = self.pages.read_image(CONSTRAINTS_KEY)
        definitions = (image["payload"] if image else {}).get(table_id, [])
        result = []
        for value in definitions:
            data = dict(value)
            if data.get("expression"):
                data["expression"] = expression_from_dict(data["expression"])
            result.append(Constraint(**data))
        return result


class Runtime:
    def __init__(self, root: str | Path, buffer_pages: int = 16) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lockfile = (self.root / "c_runtime.lock").open("a+b")
        self.closed = False
        self.wal = None
        try:
            # One writer process, multiple session/transaction threads. The OS
            # releases this advisory process lock on abrupt process exit.
            if os.name == "nt":
                import msvcrt
                self._lockfile.seek(0, os.SEEK_END)
                if self._lockfile.tell() == 0:
                    self._lockfile.write(b"0")
                    self._lockfile.flush()
                self._lockfile.seek(0)
                msvcrt.locking(self._lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.pages = ReferencePageStore(self.root, buffer_pages)
            self.wal = WAL(self.root / "wal" / "c.wal")
            self.pages.ensure_log_durable = self.wal.ensure_log_durable
            self.transactions = TransactionManager(self.pages, self.wal)
            self.recovery_summary = self.transactions.recover()
            self.catalog = CatalogBridge(self.pages)
            self.records = RecordAdapter(self.catalog, self.pages, self.transactions)
            self.indexes = IndexManager(self.pages, self.records, self.transactions)
            self.executor = QueryExecutor(self.records, self.indexes, self.transactions)
        except BaseException:
            if self.wal:
                self.wal.close()
            self._lockfile.close()
            raise

    def context(self, session_id="local", user_id="test", memory_limit_bytes=64 * 1024 * 1024):
        tx = self.transactions.begin(session_id)
        return ExecutionContext(uuid.uuid4().hex, session_id, user_id, tx.id,
                                memory_limit_bytes=memory_limit_bytes)

    def create_table(self, schema: TableSchema, constraints: list[Constraint] | None = None):
        """Independent DDL transaction for bootstrap/testing. No SQL/auth layer."""
        ctx = self.context("ddl_" + uuid.uuid4().hex)
        try:
            self.transactions.acquire_lock(ctx.tx_id, "catalog:schema", "X")
            before = self.pages.read_image(CATALOG_KEY)
            payload = dict(before["payload"]) if before else {}
            if schema.name in payload:
                raise DBError("TABLE_EXISTS", schema.name)
            names = {c.name for c in schema.columns}
            for constraint in constraints or []:
                if constraint.kind not in {"UNIQUE", "CHECK", "FOREIGN_KEY"}:
                    raise DBError("INVALID_CONSTRAINT", "Use schema.primary_key for PRIMARY KEY")
                if any(c not in names for c in constraint.columns):
                    raise DBError("INVALID_CONSTRAINT", "Unknown local column")
                if constraint.kind == "CHECK" and constraint.expression is None:
                    raise DBError("INVALID_CONSTRAINT", "CHECK needs a bound expression")
                if constraint.kind == "CHECK":
                    def validate_check(expression):
                        if expression.kind in {"Subquery", "Aggregate"}:
                            raise DBError("INVALID_CONSTRAINT", "CHECK must be a row-local scalar expression")
                        if expression.kind == "ColumnRef" and (expression.level != 0 or expression.slot_id not in names):
                            raise DBError("INVALID_CONSTRAINT", "CHECK references an unknown column")
                        for argument in expression.args:
                            validate_check(argument)
                    validate_check(constraint.expression)
                if constraint.kind == "UNIQUE" and not constraint.columns:
                    raise DBError("INVALID_CONSTRAINT", "UNIQUE needs columns")
                if constraint.kind == "FOREIGN_KEY":
                    ref = schema if constraint.ref_table == schema.name else self.catalog.table(constraint.ref_table)
                    if len(constraint.columns) != 1 or constraint.ref_columns != [ref.primary_key]:
                        raise DBError("INVALID_CONSTRAINT", "First version references one primary-key column")
                    local = next(c for c in schema.columns if c.name == constraint.columns[0])
                    remote = next(c for c in ref.columns if c.name == ref.primary_key)
                    if local.type != remote.type:
                        raise DBError("INVALID_CONSTRAINT", "Foreign key types differ")
            payload[schema.name] = schema.to_dict()
            image = dict(kind="CATALOG", payload=payload)
            self.pages.validate_image(image)
            self.transactions.write_change(ctx.tx_id, PageChange(CATALOG_KEY, before, image))
            if constraints:
                before_rules = self.pages.read_image(CONSTRAINTS_KEY)
                rules = dict(before_rules["payload"]) if before_rules else {}
                rules[schema.name] = [asdict(c) for c in constraints]
                after_rules = dict(kind="CATALOG", payload=rules)
                self.pages.validate_image(after_rules)
                self.transactions.write_change(ctx.tx_id, PageChange(CONSTRAINTS_KEY, before_rules, after_rules))
            self.transactions.commit(ctx.tx_id)
        except BaseException:
            if not self.transactions.needs_recovery:
                self.transactions.rollback(ctx.tx_id)
            raise

    def close(self):
        if self.closed:
            return
        try:
            if not self.transactions.needs_recovery:
                for tx in list(self.transactions.transactions.values()):
                    if tx.state == "ACTIVE":
                        self.transactions.rollback(tx.id)
                self.pages.flush_all()
        finally:
            self.wal.close()
            self._lockfile.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
