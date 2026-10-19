from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from edudb import (
    CatalogCommand, ColumnDef, Constraint, DBError, ExecutionContext, IndexInfo,
    Metrics, Page, PageChange, PageKey, RID, StorageService, TableSchema, ViewDef,
)
from edudb.contracts import LogRecord, ResultColumn
from edudb.examples.memory_services import MemoryLog, MemoryTransactions
from edudb.module_a.pages import CATALOG_KEY


class ContractATest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = MemoryLog()
        self.txns = MemoryTransactions(self.log)
        self.storage = StorageService(self.root, self.log, self.txns, capacity=4)
        self.txns.storage = self.storage
        self.storage.finish_recovery()
        self.schema = TableSchema("t-id", "MixedCase", [ColumnDef("c-id", "Id", "INT"), ColumnDef("c-name", "Name", "TEXT")])
        self.storage.apply_catalog_command(CatalogCommand("CREATE_TABLE", "owner", table=self.schema))

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(DBError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def context(self, session="session"):
        tx = self.txns.begin(session)
        self.txns.acquire_lock(tx.id, self.schema.id, "X", 1000)
        return ExecutionContext("query-" + tx.id, session, "owner", tx.id)

    def publish(self, prepared, ctx):
        lsns = [self.log.append_log(LogRecord(ctx.tx_id, "UPDATE", change=change)) for change in prepared.changes]
        self.storage.apply_changes(prepared, lsns)
        return lsns

    def insert(self, ctx, values=None):
        prepared = self.storage.prepare_record_change(self.schema.id, "INSERT", values=values or [1, "one"], ctx=ctx)
        self.publish(prepared, ctx)
        return prepared.rid

    def seed(self):
        ctx = self.context()
        rid = self.insert(ctx)
        self.txns.commit(ctx.tx_id)
        return rid

    def test_catalog_ids_names_snapshots_and_owners(self):
        table = self.storage.get_table("MixedCase")
        self.assertEqual(table, self.schema)
        table.columns[0].name = "changed"
        self.assertEqual(self.storage.get_table("t-id").columns[0].name, "Id")
        self.assert_code("OBJECT_NOT_FOUND", self.storage.get_table, "mixedcase")
        self.assertEqual(self.storage.list_objects()[0].owner_id, "owner")
        self.assertEqual(self.storage.catalog_version, 1)

    def test_name_policy_is_injected_not_hardcoded(self):
        self.storage.close()
        storage = StorageService(self.root, self.log, self.txns, normalize_name=str.casefold)
        self.txns.storage = storage
        storage.finish_recovery()
        self.assertEqual(storage.get_table("mixedcase").id, "t-id")
        storage.close()

    def test_view_dependencies_block_drop(self):
        view = ViewDef("view-id", "StudentView", "owner", {"kind": "Select"},
                       [ResultColumn("slot-1", "Id", "INT", True)], [self.schema.id])
        self.storage.apply_catalog_command(CatalogCommand("CREATE_VIEW", "owner", view=view))
        snapshot = self.storage.get_view("StudentView")
        snapshot.dependencies.clear()
        self.assertEqual(self.storage.get_view("view-id").dependencies, [self.schema.id])
        self.assert_code("OBJECT_REFERENCED", self.storage.apply_catalog_command,
                         CatalogCommand("DROP_TABLE", "owner", object_id=self.schema.id))
        self.storage.apply_catalog_command(CatalogCommand("DROP_VIEW", "owner", object_id=view.id))
        self.storage.apply_catalog_command(CatalogCommand("DROP_TABLE", "owner", object_id=self.schema.id))
        self.assertEqual(self.storage.list_objects(), [])

    def test_view_invalid_dependencies_and_security_mode(self):
        for deps, security in ((["missing"], "INVOKER"), (["v"], "INVOKER"), ([self.schema.id], "DEFINER")):
            view = ViewDef("v", "view", "owner", {}, [], deps, security)
            with self.assertRaises(DBError):
                self.storage.apply_catalog_command(CatalogCommand("CREATE_VIEW", "owner", view=view))
        self.assertEqual(len(self.storage.list_objects()), 1)
        self.assertFalse(self.txns.has_active_transactions())

    def test_constraints_require_c_capability(self):
        schema = TableSchema("second", "Second", [ColumnDef("second-col", "id", "INT")],
                             [Constraint("PRIMARY_KEY", ["second-col"])])
        self.assert_code("UNSUPPORTED_CONSTRAINT", self.storage.apply_catalog_command,
                         CatalogCommand("CREATE_TABLE", "owner", table=schema))
        self.assertEqual(len(self.storage.list_objects()), 1)

    def test_ddl_rejects_business_transaction(self):
        ctx = self.context()
        self.assert_code("DDL_TRANSACTION_CONFLICT", self.storage.apply_catalog_command,
                         CatalogCommand("DROP_TABLE", "owner", object_id=self.schema.id))
        tx = self.txns.get_transaction(ctx.tx_id, ctx.session_id)
        self.assert_code("DDL_TRANSACTION_CONFLICT", self.storage.apply_catalog_command,
                         CatalogCommand("DROP_TABLE", "owner", object_id=self.schema.id, transaction=tx))
        self.txns.rollback(ctx.tx_id)

    def test_prepare_discard_does_not_publish_allocation_or_rows(self):
        ctx = self.context()
        prepared = self.storage.prepare_record_change(self.schema.id, "INSERT", values=[1, "one"], ctx=ctx)
        self.assertEqual(self.storage.diagnostics()["prepared_changes"], 1)
        self.storage.discard_changes(prepared)
        self.assertEqual(self.storage.get_statistics(self.schema.id).row_count, 0)
        self.assertEqual(self.storage.get_statistics(self.schema.id).page_count, 0)
        self.assertEqual(self.storage.diagnostics()["pinned_handles"], 0)
        self.assert_code("INVALID_PREPARED_CHANGE", self.storage.discard_changes, prepared)
        self.txns.rollback(ctx.tx_id)

    def test_record_list_values_no_sql_conversion_or_constraint_enforcement(self):
        ctx = self.context()
        rid = self.insert(ctx, [True, None])
        self.assertEqual(self.storage.read_record(self.schema.id, rid, ctx).values, [True, None])
        self.assertEqual(list(self.storage.list_record_ids(self.schema.id, ctx)), [rid])
        copied = self.storage.read_record(self.schema.id, rid, ctx)
        copied.values[0] = 99
        self.assertEqual(self.storage.read_record(self.schema.id, rid, ctx).values[0], True)
        self.txns.commit(ctx.tx_id)

    def test_insert_update_delete_preserve_other_rids(self):
        ctx = self.context()
        first = self.insert(ctx, [1, "one"])
        second = self.insert(ctx, [2, "two"])
        self.publish(self.storage.prepare_record_change(self.schema.id, "UPDATE", second, [2, "changed"], ctx), ctx)
        self.publish(self.storage.prepare_record_change(self.schema.id, "DELETE", first, ctx=ctx), ctx)
        self.assert_code("RECORD_NOT_FOUND", self.storage.read_record, self.schema.id, first, ctx)
        self.assertEqual(list(self.storage.list_record_ids(self.schema.id, ctx)), [second])
        self.assertEqual(self.storage.read_record(self.schema.id, second, ctx).values, [2, "changed"])
        self.txns.commit(ctx.tx_id)
        self.assertEqual(self.storage.get_statistics(self.schema.id).row_count, 1)
        self.assertIsNone(self.storage.get_statistics(self.schema.id).column_stats)

    def test_large_records_new_pages_and_restart(self):
        ctx = self.context()
        rids = [self.insert(ctx, [i, "x" * 1100]) for i in range(8)]
        self.assertGreater(len({rid.page_id for rid in rids}), 1)
        self.txns.commit(ctx.tx_id)
        self.storage.close()
        reopened = StorageService(self.root, self.log, self.txns)
        self.txns.storage = reopened
        reopened.finish_recovery()
        ctx = self.context("readback")
        self.assertEqual([reopened.read_record(self.schema.id, rid, ctx).values[0] for rid in rids], list(range(8)))
        self.txns.commit(ctx.tx_id)
        reopened.close()

    def test_oversized_and_memory_limited_prepare_leave_no_pins(self):
        ctx = self.context()
        self.assert_code("PAGE_FULL", self.storage.prepare_record_change, self.schema.id, "INSERT", values=[1, "x" * 6000], ctx=ctx)
        ctx.memory_limit_bytes = 1
        self.assert_code("RESOURCE_LIMIT", self.storage.prepare_record_change, self.schema.id, "INSERT", values=[1, "a"], ctx=ctx)
        self.assertEqual(self.storage.diagnostics()["pinned_handles"], 0)
        self.assertEqual(self.storage.get_statistics(self.schema.id).row_count, 0)
        self.txns.rollback(ctx.tx_id)

    def test_unknown_lsn_and_mismatched_transaction_cannot_publish(self):
        ctx = self.context()
        prepared = self.storage.prepare_record_change(self.schema.id, "INSERT", values=[1, "one"], ctx=ctx)
        self.assert_code("WAL_NOT_FOUND", self.storage.apply_changes, prepared, [999, 1000])
        self.assertEqual(self.storage.diagnostics()["pinned_handles"], 0)
        prepared = self.storage.prepare_record_change(self.schema.id, "INSERT", values=[1, "one"], ctx=ctx)
        lsns = [self.log.append_log(LogRecord("other-tx", "UPDATE", change=c)) for c in prepared.changes]
        self.assert_code("WAL_MISMATCH", self.storage.apply_changes, prepared, lsns)
        self.assertEqual(self.storage.get_statistics(self.schema.id).row_count, 0)
        self.txns.rollback(ctx.tx_id)

    def test_failed_batch_validation_publishes_no_partial_pages(self):
        ctx = self.context()
        prepared = self.storage.prepare_record_change(self.schema.id, "INSERT", values=[1, "one"], ctx=ctx)
        first = self.log.append_log(LogRecord(ctx.tx_id, "UPDATE", change=prepared.changes[0]))
        self.assert_code("WAL_NOT_FOUND", self.storage.apply_changes, prepared, [first, 999])
        self.assertEqual(self.storage.get_statistics(self.schema.id).page_count, 0)
        self.assertEqual(self.storage.diagnostics()["pinned_handles"], 0)
        self.txns.rollback(ctx.tx_id)

    def test_mutated_prepared_images_are_rejected_and_released(self):
        ctx = self.context()
        prepared = self.storage.prepare_record_change(self.schema.id, "INSERT", values=[1, "one"], ctx=ctx)
        lsns = [self.log.append_log(LogRecord(ctx.tx_id, "UPDATE", change=c)) for c in prepared.changes]
        prepared.changes[0].after_image.payload["slots"][0][1] = "tampered"
        self.assert_code("INVALID_PREPARED_CHANGE", self.storage.apply_changes, prepared, lsns)
        self.assertEqual(self.storage.diagnostics()["pinned_handles"], 0)
        self.txns.rollback(ctx.tx_id)

    def test_fixed_handles_and_release_lifetime(self):
        rid = self.seed()
        key = PageKey(self.schema.id, rid.page_id)
        handle = self.storage.fetch_page(key, "READ", Metrics())
        self.assert_code("PAGE_BUSY", self.storage.fetch_page, key, "WRITE", Metrics())
        self.assert_code("PAGE_PINNED", self.storage.flush_page, key)
        before = self.storage.diagnostics()["dirty_pages"]
        self.storage.flush_all()
        self.assertGreaterEqual(self.storage.diagnostics()["dirty_pages"], 1)
        self.assertGreaterEqual(before, 1)
        handle.page.payload["slots"][0][1] = "not published"
        self.storage.release_page(handle)
        self.assert_code("INVALID_HANDLE", self.storage.release_page, handle)
        with self.assertRaises(DBError):
            _ = handle.page
        ctx = self.context()
        self.assertEqual(self.storage.read_record(self.schema.id, rid, ctx).values[1], "one")
        self.txns.commit(ctx.tx_id)

    def test_prepared_handles_can_only_be_released_as_a_group(self):
        ctx = self.context()
        prepared = self.storage.prepare_record_change(self.schema.id, "INSERT", values=[1, "one"], ctx=ctx)
        self.assert_code("PAGE_BUSY", self.storage.release_page, prepared.handles[0])
        self.storage.discard_changes(prepared)
        self.assertEqual(self.storage.diagnostics()["pinned_handles"], 0)
        self.txns.rollback(ctx.tx_id)

    def test_wal_barrier_precedes_page_file_write(self):
        rid = self.seed()
        key = PageKey(self.schema.id, rid.page_id)
        events = []
        write = self.storage._files.write
        durable = self.log.ensure_log_durable
        def barrier(lsn):
            events.append(("wal", lsn))
            durable(lsn)
        def page_write(page):
            events.append(("page", page.page_lsn))
            write(page)
        with patch.object(self.log, "ensure_log_durable", side_effect=barrier), patch.object(self.storage._files, "write", side_effect=page_write):
            self.storage.flush_page(key)
        self.assertEqual(events[0][0], "wal")
        self.assertEqual(events[1], ("page", events[0][1]))

    def test_failed_wal_barrier_prevents_write_and_retains_dirty(self):
        rid = self.seed()
        with patch.object(self.log, "ensure_log_durable", side_effect=DBError("LOG_IO_ERROR", "failed")), patch.object(self.storage._files, "write") as write:
            self.assert_code("LOG_IO_ERROR", self.storage.flush_page, PageKey(self.schema.id, rid.page_id))
            write.assert_not_called()
        self.assertGreater(self.storage.diagnostics()["dirty_pages"], 0)
        self.storage.flush_all()
        self.assertEqual(self.storage.diagnostics()["dirty_pages"], 0)

    def test_file_write_failure_keeps_dirty_and_retries(self):
        rid = self.seed()
        key = PageKey(self.schema.id, rid.page_id)
        with patch.object(self.storage._files, "write", side_effect=OSError("full")):
            self.assert_code("IO_ERROR", self.storage.flush_page, key)
        self.assertGreater(self.storage.diagnostics()["dirty_pages"], 0)
        self.storage.flush_page(key)

    def test_reset_requires_exclusive_idle_and_no_handles(self):
        rid = self.seed()
        self.assert_code("EXPERIMENT_REQUIRED", self.storage.reset_cache)
        ctx = self.context()
        with self.assertRaises(DBError):
            with self.storage.exclusive_experiment():
                pass
        self.txns.commit(ctx.tx_id)
        handle = self.storage.fetch_page(PageKey(self.schema.id, rid.page_id), "READ", Metrics())
        with self.storage.exclusive_experiment():
            self.assert_code("PAGE_PINNED", self.storage.reset_cache)
        self.storage.release_page(handle)
        with self.storage.exclusive_experiment():
            self.storage.reset_cache()
        self.assertEqual(self.storage.diagnostics()["cached_keys"], [])

    def test_metrics_are_per_query_and_data_reads_exclude_cache_hits(self):
        rid = self.seed()
        with self.storage.exclusive_experiment():
            self.storage.reset_cache()
        first, second = Metrics(), Metrics()
        handle = self.storage.fetch_page(PageKey(self.schema.id, rid.page_id), "READ", first)
        self.storage.release_page(handle)
        handle = self.storage.fetch_page(PageKey(self.schema.id, rid.page_id), "READ", second)
        self.storage.release_page(handle)
        self.assertEqual((first.data_page_reads, first.cache_misses), (1, 1))
        self.assertEqual((second.data_page_reads, second.cache_hits), (0, 1))

    def test_allocation_and_records_rollback_with_clr(self):
        ctx = self.context()
        rid = self.insert(ctx)
        self.storage.flush_all()
        self.txns.rollback(ctx.tx_id)
        self.assertEqual(self.storage.get_statistics(self.schema.id).page_count, 0)
        ctx = self.context("again")
        again = self.insert(ctx, [2, "second"])
        self.assertEqual(again, rid)
        self.assertEqual(self.storage.read_record(self.schema.id, rid, ctx).values, [2, "second"])
        self.txns.commit(ctx.tx_id)

    def test_startup_recovery_gates_access_and_replays_idempotently(self):
        rid = self.seed()
        self.storage.flush_all()
        self.storage._files.path(PageKey(self.schema.id, rid.page_id)).unlink()
        restarted = StorageService(self.root, self.log, self.txns)
        self.txns.storage = restarted
        self.assert_code("DATABASE_NOT_READY", restarted.get_table, self.schema.id)
        for record in list(self.log.records.values()):
            if record.kind == "UPDATE":
                restarted.apply_recovery_change(record.change, record.lsn, "REDO")
        bytes_before = restarted._files.path(PageKey(self.schema.id, rid.page_id)).read_bytes()
        for record in list(self.log.records.values()):
            if record.kind == "UPDATE":
                restarted.apply_recovery_change(record.change, record.lsn, "REDO")
        self.assertEqual(restarted._files.path(PageKey(self.schema.id, rid.page_id)).read_bytes(), bytes_before)
        restarted.finish_recovery()
        self.assertEqual(restarted.get_statistics(self.schema.id).row_count, 1)
        self.assert_code("RECOVERY_MODE_REQUIRED", restarted.apply_recovery_change, record.change, record.lsn, "REDO")
        restarted.close()

    def test_missing_allocated_page_blocks_finish_recovery(self):
        rid = self.seed()
        self.storage.close()
        self.storage._files.path(PageKey(self.schema.id, rid.page_id)).unlink()
        restarted = StorageService(self.root, self.log, self.txns)
        self.assert_code("PAGE_NOT_FOUND", restarted.finish_recovery)
        self.assertEqual(restarted.diagnostics()["mode"], "RECOVERING")

    def test_live_rollback_requires_matching_clr_and_transaction(self):
        ctx = self.context()
        prepared = self.storage.prepare_record_change(self.schema.id, "INSERT", values=[1, "one"], ctx=ctx)
        lsns = self.publish(prepared, ctx)
        tx = self.txns.get_transaction(ctx.tx_id, ctx.session_id)
        self.assert_code("WAL_MISMATCH", self.storage.apply_rollback_change, prepared.changes[0], lsns[0], tx)
        self.txns.rollback(ctx.tx_id)

    def test_index_page_protocol_and_catalog_publish_share_ddl_transaction(self):
        with self.storage.catalog_transaction() as tx:
            self.storage.register_index_file("index-id", tx)
            key = self.storage.allocate_page("index-id", tx)
            ctx = ExecutionContext("build-index", tx.session_id, "owner", tx.id)
            handle = self.storage.fetch_page(key, "READ", ctx.metrics)
            before = handle.page
            self.storage.release_page(handle)
            after = deepcopy(before)
            after.payload = {"leaf": True, "keys": [1], "rids": [[0, 0]]}
            prepared = self.storage.prepare_index_changes([PageChange(key, before, after)], ctx)
            self.publish(prepared, ctx)
            index = IndexInfo("index-id", "ix", self.schema.id, ["c-id"], False, key.page_id)
            self.storage.apply_catalog_command(CatalogCommand("CREATE_INDEX", "owner", index=index, transaction=tx))
        table = self.storage.get_table(self.schema.id)
        self.assertEqual(table.indexes[0], index)
        self.assertEqual(table.version, 2)
        with self.storage.exclusive_experiment():
            self.storage.reset_cache()
        metrics = Metrics()
        handle = self.storage.fetch_page(key, "READ", metrics)
        self.assertTrue(handle.page.payload["leaf"])
        self.storage.release_page(handle)
        self.assertEqual(metrics.index_page_reads, 1)
        self.assertEqual(metrics.data_page_reads, 0)
        self.assert_code("OBJECT_REFERENCED", self.storage.apply_catalog_command, CatalogCommand("DROP_TABLE", "owner", object_id=self.schema.id))
        self.storage.apply_catalog_command(CatalogCommand("DROP_INDEX", "owner", object_id=index.id))
        self.assertEqual(self.storage.get_table(self.schema.id).indexes, [])

    def test_failed_index_ddl_rolls_back_file_allocation_and_catalog(self):
        with self.assertRaises(DBError):
            with self.storage.catalog_transaction() as tx:
                self.storage.register_index_file("failed-index", tx)
                self.storage.allocate_page("failed-index", tx)
                raise DBError("BUILD_FAILED", "simulated index construction failure")
        self.assertEqual(self.storage.get_table(self.schema.id).indexes, [])
        self.assert_code("OBJECT_NOT_FOUND", self.storage.fetch_page, PageKey("failed-index", 0), "READ", Metrics())
        self.assertFalse(self.txns.has_active_transactions())

    def test_failed_catalog_log_does_not_publish_or_leak_handles(self):
        append = self.log.append_log
        def fail_update(record):
            if record.kind == "UPDATE":
                raise DBError("LOG_IO_ERROR", "append failed")
            return append(record)
        with patch.object(self.log, "append_log", side_effect=fail_update):
            self.assert_code("LOG_IO_ERROR", self.storage.apply_catalog_command,
                             CatalogCommand("DROP_TABLE", "owner", object_id=self.schema.id))
        self.assertEqual(self.storage.get_table(self.schema.id).id, self.schema.id)
        self.assertEqual(self.storage.diagnostics()["pinned_handles"], 0)

    def test_uncertain_ddl_commit_disables_access_without_guessing_rollback(self):
        with patch.object(self.txns, "commit", side_effect=OSError("commit result unknown")), patch.object(self.txns, "rollback") as rollback:
            self.assert_code("COMMIT_UNCERTAIN", self.storage.apply_catalog_command,
                             CatalogCommand("DROP_TABLE", "owner", object_id=self.schema.id))
            rollback.assert_not_called()
        self.assert_code("DATABASE_NOT_READY", self.storage.list_objects)

    def test_sync_uses_fsync_and_shutdown_does_not_commit(self):
        self.seed()
        self.storage.flush_all()
        with patch("edudb.module_a.pages.os.fsync", wraps=__import__("os").fsync) as fsync:
            self.storage.sync_files([self.schema.id, CATALOG_KEY.file_id])
            self.assertGreaterEqual(fsync.call_count, 2)
        ctx = self.context()
        self.assert_code("ACTIVE_RESOURCES", self.storage.close)
        self.txns.rollback(ctx.tx_id)
        self.storage.close()
        self.storage.close()

    def test_foreign_session_unknown_rid_and_readonly_iteration_cleanup(self):
        rid = self.seed()
        ctx = self.context()
        wrong = deepcopy(ctx)
        wrong.session_id = "another"
        self.assert_code("INVALID_TRANSACTION", self.storage.read_record, self.schema.id, rid, wrong)
        self.assert_code("RECORD_NOT_FOUND", self.storage.read_record, self.schema.id, RID(0, 999), ctx)
        iterator = self.storage.list_record_ids(self.schema.id, ctx)
        self.assertEqual(next(iterator), rid)
        iterator.close()
        self.assertEqual(self.storage.diagnostics()["pinned_handles"], 0)
        self.txns.commit(ctx.tx_id)

    def test_lru_cannot_evict_fixed_frames_and_writeback_failure_keeps_frame(self):
        ctx = self.context()
        for i in range(5):
            self.storage.allocate_page(self.schema.id, self.txns.get_transaction(ctx.tx_id, ctx.session_id))
        self.txns.commit(ctx.tx_id)
        self.storage.flush_all()
        handles = [self.storage.fetch_page(PageKey(self.schema.id, i), "READ", Metrics()) for i in range(3)]
        # A's private catalog frame occupies the fourth frame while metadata is read.
        handle = self.storage.fetch_page(PageKey(self.schema.id, 3), "READ", Metrics())
        handles.append(handle)
        self.assert_code("BUFFER_FULL", self.storage.fetch_page, PageKey(self.schema.id, 4), "READ", Metrics())
        for handle in handles:
            self.storage.release_page(handle)
        handle = self.storage.fetch_page(PageKey(self.schema.id, 4), "READ", Metrics())
        self.storage.release_page(handle)

    def test_corrupt_file_read_is_counted_but_not_cached(self):
        self.seed()
        with self.storage.exclusive_experiment():
            self.storage.reset_cache()
        key = PageKey(self.schema.id, 0)
        self.storage._files.path(key).write_bytes(b"corrupt")
        metrics = Metrics()
        self.assert_code("CORRUPT_PAGE", self.storage.fetch_page, key, "READ", metrics)
        self.assertEqual(metrics.data_page_reads, 1)
        self.assertNotIn(key, self.storage.diagnostics()["cached_keys"])
        self.assertEqual(self.storage.diagnostics()["pinned_handles"], 0)

    def test_retired_view_id_cannot_be_reused_after_reopen(self):
        view = ViewDef("retired-view", "V", "owner", {}, [], [self.schema.id])
        self.storage.apply_catalog_command(CatalogCommand("CREATE_VIEW", "owner", view=view))
        self.storage.apply_catalog_command(CatalogCommand("DROP_VIEW", "owner", object_id=view.id))
        self.storage.close()
        self.storage = StorageService(self.root, self.log, self.txns)
        self.txns.storage = self.storage
        self.storage.finish_recovery()
        self.assert_code("OBJECT_EXISTS", self.storage.apply_catalog_command,
                         CatalogCommand("CREATE_VIEW", "owner", view=view))

    def test_interrupted_clr_can_be_retried_and_replayed(self):
        ctx = self.context()
        self.insert(ctx)
        self.storage.flush_all()
        with patch.object(self.storage, "apply_rollback_change", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.txns.rollback(ctx.tx_id)
        self.txns.rollback(ctx.tx_id)
        self.assertEqual(self.storage.get_statistics(self.schema.id).row_count, 0)
        self.storage.close()
        self.storage = StorageService(self.root, self.log, self.txns)
        self.txns.storage = self.storage
        for record in list(self.log.records.values()):
            if record.kind in {"UPDATE", "CLR"}:
                self.storage.apply_recovery_change(record.change, record.lsn, "REDO")
                self.storage.apply_recovery_change(record.change, record.lsn, "REDO")
        self.storage.finish_recovery()
        self.assertEqual(self.storage.get_statistics(self.schema.id).row_count, 0)

    def test_reject_legacy_directory_and_missing_c_services(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "storage.json").write_text('{"format":"edudb.storage","version":2}', encoding="utf-8")
            self.assert_code("FORMAT_MISMATCH", StorageService, root, self.log, self.txns)
        self.assert_code("INVALID_ARGUMENT", StorageService, self.root, None, self.txns)


if __name__ == "__main__":
    unittest.main()
