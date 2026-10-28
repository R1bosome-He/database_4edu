"""Reference driver that runs compiled SQL against the C runtime.

This is a development/demonstration convenience, NOT D's authenticated request
handler: it performs no authentication or authorization. It exists so B can be
exercised end-to-end through module C's real executor, WAL and recovery.
"""
from __future__ import annotations

import uuid
from contextlib import nullcontext

from ..c_api import ExecutionContext, QueryResult, ResultColumn
from .catalog import RuntimeCatalog
from .compiler import CompileOptions, compile_sql


class SqlSession:
    def __init__(self, runtime, user_id: str = "tester", session_id: str | None = None,
                 optimizer_enabled: bool = True) -> None:
        self.runtime = runtime
        self.catalog = RuntimeCatalog(runtime)
        self.user_id = user_id
        self.session_id = session_id or ("sql_" + uuid.uuid4().hex)
        self.optimizer_enabled = optimizer_enabled
        self.active_tx_id: str | None = None

    # --- transaction handling ------------------------------------------
    def _context(self):
        if self.active_tx_id is not None:
            return ExecutionContext(uuid.uuid4().hex, self.session_id, self.user_id, self.active_tx_id), False
        tx = self.runtime.transactions.begin(self.session_id)
        return ExecutionContext(uuid.uuid4().hex, self.session_id, self.user_id, tx.id), True

    def execute(self, sql: str, optimizer_enabled: bool | None = None) -> QueryResult:
        with getattr(self.runtime, "request_lock", nullcontext()):
            enabled = self.optimizer_enabled if optimizer_enabled is None else optimizer_enabled
            compiled = compile_sql(sql, self.catalog, CompileOptions(enabled))
            if compiled.kind in {"CREATE_TABLE", "CREATE_INDEX", "CREATE_VIEW", "DROP_VIEW"} and self.active_tx_id is not None:
                from ..c_api import DBError
                raise DBError("DDL_TRANSACTION_CONFLICT", "DDL requires an independent transaction")
            return self._dispatch(compiled)

    def _dispatch(self, compiled) -> QueryResult:
        kind = compiled.kind
        if kind in {"SELECT", "INSERT", "UPDATE", "DELETE"}:
            return self._run_plan(compiled)
        if kind == "EXPLAIN":
            rows = [[line] for line in (compiled.explain_text or "").splitlines()]
            return QueryResult(compiled.output_columns, rows, message="EXPLAIN")
        if kind == "CREATE_TABLE":
            self.runtime.create_table(compiled.table_schema, compiled.constraints or None)
            return QueryResult([], [], message=f"Table {compiled.table_schema.name} created")
        if kind == "CREATE_VIEW":
            self.runtime.create_view(compiled.view_name,compiled.view_sql,compiled.output_columns,
                                     compiled.view_dependencies,self.user_id)
            return QueryResult([],[],message="视图已创建")
        if kind == "DROP_VIEW":
            self.runtime.drop_view(compiled.view_name,self.user_id)
            return QueryResult([],[],message="视图已删除")
        if kind == "CREATE_INDEX":
            if hasattr(self.runtime, "create_index"):
                info = self.runtime.create_index(compiled.index_spec, self.session_id, self.user_id)
                return QueryResult([], [], message=f"Index {info.name} created")
            ctx, owned = self._context()
            info = self.runtime.executor.create_index(compiled.index_spec, ctx)
            if owned:
                self.runtime.transactions.commit(ctx.tx_id)
            return QueryResult([], [], message=f"Index {info.name} created")
        if kind == "BEGIN":
            if self.active_tx_id is not None:
                from ..c_api import DBError
                raise DBError("TRANSACTION_ACTIVE", "A transaction is already in progress")
            self.active_tx_id = self.runtime.transactions.begin(self.session_id).id
            return QueryResult([], [], message="BEGIN")
        if kind in {"COMMIT", "ROLLBACK"}:
            return self._finish_transaction(kind)
        from ..c_api import DBError
        raise DBError("INTERNAL_ERROR", f"Driver cannot dispatch {kind}")

    def _run_plan(self, compiled) -> QueryResult:
        ctx, owned = self._context()
        try:
            result = self.runtime.executor.execute(compiled.plan, ctx)
        except Exception:
            self.active_tx_id = None   # executor aborts the transaction on error
            raise
        if owned:
            self.runtime.transactions.commit(ctx.tx_id)
        return result

    def _finish_transaction(self, kind) -> QueryResult:
        from ..c_api import DBError
        if self.active_tx_id is None:
            raise DBError("NO_TRANSACTION", f"{kind} without an active transaction")
        tx_id, self.active_tx_id = self.active_tx_id, None
        if kind == "COMMIT":
            self.runtime.transactions.commit(tx_id)
        else:
            self.runtime.transactions.rollback(tx_id)
        return QueryResult([], [], message=kind)
