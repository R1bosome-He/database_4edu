from __future__ import annotations

import tempfile
from pathlib import Path

from ..contracts import CatalogCommand, ColumnDef, ExecutionContext, LogRecord, TableSchema
from ..module_a import StorageService
from .memory_services import MemoryLog, MemoryTransactions


def run() -> None:
    print("A contract demo: using in-memory C test doubles, NOT a persistent WAL implementation.")
    with tempfile.TemporaryDirectory(prefix="edudb-contract-") as directory:
        log = MemoryLog()
        transactions = MemoryTransactions(log)
        storage = StorageService(Path(directory), log, transactions, capacity=4)
        transactions.storage = storage
        storage.finish_recovery()
        schema = TableSchema("students-id", "Students", [ColumnDef("column-id", "id", "INT"), ColumnDef("column-name", "name", "TEXT")])
        storage.apply_catalog_command(CatalogCommand("CREATE_TABLE", "owner", table=schema))
        tx = transactions.begin("demo-session")
        ctx = ExecutionContext("query-1", tx.session_id, "owner", tx.id)
        transactions.acquire_lock(tx.id, schema.id, "X", 1000)
        prepared = storage.prepare_record_change(schema.id, "INSERT", values=[1, "Ada"], ctx=ctx)
        print(f"Prepared RID: {prepared.rid}; fixed handles: {storage.diagnostics()['pinned_handles']}")
        lsns = [log.append_log(LogRecord(tx.id, "UPDATE", change=change)) for change in prepared.changes]
        storage.apply_changes(prepared, lsns)
        print(f"Record: {storage.read_record(schema.id, prepared.rid, ctx).values}")
        print(f"Fixed handles after apply: {storage.diagnostics()['pinned_handles']}")
        transactions.commit(tx.id)
        storage.flush_all()
        print(f"Statistics: {storage.get_statistics(schema.id)}")
        storage.close()
        reopened = StorageService(directory, log, transactions)
        transactions.storage = reopened
        reopened.finish_recovery()
        print(f"Objects after reopen: {reopened.list_objects()}")
        reopened.close()
    print("A contract demo complete; temporary files removed.")
