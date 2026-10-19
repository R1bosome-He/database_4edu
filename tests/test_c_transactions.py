from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import patch

from edudb.c_api import DBError, IndexSpec, PageChange, PageKey, PlanNode, RangeSpec, ResultColumn
from edudb.integration.runtime import Runtime
from edudb.models import Column, TableSchema
from edudb.transactions import WAL


class CTransactionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Runtime(self.root)
        self.app.create_table(TableSchema("t", (Column("id", "INT"), Column("v", "TEXT")), "id"))

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def insert(self, ctx, ident=1, value="one"):
        return self.app.executor.execute(PlanNode("i", "Insert", properties=dict(
            table_id="t", rows=[dict(id=ident, v=value)])), ctx)

    def query(self):
        ctx = self.app.context("read")
        result = self.app.executor.execute(PlanNode("s", "SeqScan", output_columns=[
            ResultColumn("id", "id", "INT"), ResultColumn("v", "v", "TEXT")],
            properties=dict(table_id="t")), ctx)
        self.app.transactions.commit(ctx.tx_id)
        return result.rows

    def reopen(self):
        self.app.close()
        self.app = Runtime(self.root)

    def test_begin_owner_and_duplicate_begin(self):
        ctx = self.app.context("a")
        with self.assertRaises(DBError):
            self.app.context("a")
        with self.assertRaises(DBError):
            self.app.transactions.get_transaction(ctx.tx_id, "b")
        self.app.transactions.rollback(ctx.tx_id)
        self.assertEqual(self.app.transactions.transactions[ctx.tx_id].state, "ABORTED")

    def test_commit_rollback_reopen(self):
        ctx = self.app.context("a")
        self.insert(ctx)
        self.app.transactions.commit(ctx.tx_id)
        ctx = self.app.context("b")
        self.insert(ctx, 2)
        self.app.transactions.rollback(ctx.tx_id)
        self.reopen()
        self.assertEqual(self.query(), [[1, "one"]])
        self.reopen()
        self.assertEqual(self.query(), [[1, "one"]])

    def test_close_aborts_active_transaction(self):
        ctx = self.app.context()
        self.insert(ctx)
        self.reopen()
        self.assertEqual(self.query(), [])

    def test_lock_waits_until_commit(self):
        a, b = self.app.context("a"), self.app.context("b")
        self.app.transactions.acquire_lock(a.tx_id, "table:t", "X")
        attempting, acquired = threading.Event(), threading.Event()
        errors = []
        def worker():
            attempting.set()
            try:
                self.app.transactions.acquire_lock(b.tx_id, "table:t", "S", 1500)
                acquired.set()
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(attempting.wait(1))
        self.assertFalse(acquired.wait(.05))
        self.app.transactions.commit(a.tx_id)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertTrue(acquired.is_set())
        self.app.transactions.commit(b.tx_id)

    def test_lock_upgrade_timeout_aborts_victim(self):
        a, b = self.app.context("a"), self.app.context("b")
        for ctx in (a, b):
            self.app.transactions.acquire_lock(ctx.tx_id, "table:t", "S")
        with self.assertRaises(DBError) as raised:
            self.app.transactions.acquire_lock(b.tx_id, "table:t", "X", 0)
        self.assertEqual(raised.exception.code, "LOCK_TIMEOUT")
        self.assertEqual(raised.exception.transaction_effect, "TRANSACTION_ABORTED")
        self.app.transactions.acquire_lock(a.tx_id, "table:t", "X", 0)
        self.app.transactions.commit(a.tx_id)

    def test_failed_page_apply_requires_recovery_and_undo(self):
        ctx = self.app.context()
        original = self.app.pages.apply_image
        def failing(key, image, lsn):
            original(key, image, lsn)
            raise OSError("simulated failure after page write")
        with patch.object(self.app.pages, "apply_image", side_effect=failing):
            with self.assertRaises(DBError):
                self.insert(ctx)
        self.assertTrue(self.app.transactions.needs_recovery)
        with self.assertRaises(DBError):
            self.app.context("new")
        self.reopen()
        self.assertEqual(self.query(), [])

    def test_failed_commit_does_not_report_success(self):
        ctx = self.app.context()
        self.insert(ctx)
        with patch.object(self.app.wal, "ensure_log_durable", side_effect=OSError("fsync failed")):
            with self.assertRaises(OSError):
                self.app.transactions.commit(ctx.tx_id)
        self.assertTrue(self.app.transactions.needs_recovery)
        # A commit with an I/O failure has an uncertain outcome: do not assert
        # rollback here. On restart the complete durable COMMIT decides outcome.

    def test_wal_is_durable_before_page_apply(self):
        ctx = self.app.context()
        original = self.app.pages.apply_image
        observed = []
        def checked(key, image, lsn):
            self.assertGreaterEqual(self.app.wal.durable_lsn, lsn)
            self.assertEqual(self.app.wal.get(lsn)["kind"], "UPDATE")
            observed.append(lsn)
            return original(key, image, lsn)
        with patch.object(self.app.pages, "apply_image", side_effect=checked):
            self.insert(ctx)
        self.assertTrue(observed)
        self.app.transactions.commit(ctx.tx_id)

    def test_checkpoint_requires_quiescence(self):
        ctx = self.app.context()
        with self.assertRaises(DBError):
            self.app.transactions.checkpoint()
        self.app.transactions.rollback(ctx.tx_id)
        self.app.transactions.checkpoint()
        self.assertEqual(self.app.wal.records[-1]["kind"], "CHECKPOINT")
        self.reopen()
        self.assertEqual(self.query(), [])

    def test_torn_wal_tail_is_truncated(self):
        self.app.close()
        path = self.root / "wal" / "c.wal"
        size = path.stat().st_size
        with path.open("ab") as stream:
            stream.write(b"\x00\x00\x00")
        self.app = Runtime(self.root)
        self.assertEqual(path.stat().st_size, size)
        self.assertEqual(self.query(), [])

    def test_complete_corrupt_log_is_rejected(self):
        self.app.close()
        path = self.root / "wal" / "c.wal"
        raw = bytearray(path.read_bytes())
        raw[10] ^= 1
        path.write_bytes(raw)
        with self.assertRaises(DBError) as raised:
            Runtime(self.root)
        self.assertEqual(raised.exception.code, "WAL_CORRUPTION")

    def test_process_lock_rejects_second_runtime(self):
        with self.assertRaises((OSError, DBError)):
            Runtime(self.root)

    def test_stale_before_image_is_rejected(self):
        ctx = self.app.context()
        self.insert(ctx)
        with self.assertRaises(DBError) as raised:
            self.app.transactions.write_change(ctx.tx_id, PageChange(PageKey("ct_t", 0), None, None))
        self.assertEqual(raised.exception.code, "STALE_PAGE")
        self.app.transactions.rollback(ctx.tx_id)

    def test_redo_reconstructs_missing_committed_page(self):
        ctx = self.app.context()
        self.insert(ctx)
        self.app.transactions.commit(ctx.tx_id)
        self.app.close()
        # Only a generated temporary test page is removed. Simulates a data page
        # not being present while its committed full-image WAL remains available.
        page = self.root / "c_pages_v1" / "ct_t" / "00000000.page"
        self.assertTrue(page.is_relative_to(self.root))
        page.unlink()
        self.app = Runtime(self.root)
        self.assertEqual(self.query(), [[1, "one"]])

    def test_index_survives_reopen_and_incremental_delete(self):
        ctx = self.app.context()
        for i in range(35):
            self.insert(ctx, i, "v")
        info = self.app.executor.create_index(IndexSpec("ix", "t", ["id"], True), ctx)
        self.app.transactions.commit(ctx.tx_id)
        self.reopen()
        ctx = self.app.context()
        self.assertEqual(len(list(self.app.indexes.search_index(info.id, RangeSpec(5, 10), ctx))), 6)
        columns = [ResultColumn("id", "id", "INT"), ResultColumn("v", "v", "TEXT")]
        self.app.executor.execute(PlanNode("d", "Delete", properties=dict(table_id="t", input_columns=columns)), ctx)
        self.app.transactions.commit(ctx.tx_id)
        self.reopen()
        ctx = self.app.context()
        self.assertEqual(list(self.app.indexes.search_index(info.id, RangeSpec(), ctx)), [])
        self.app.transactions.commit(ctx.tx_id)

    def _child_crash(self, mode):
        self.app.close()
        script = textwrap.dedent('''
            import os, sys
            from edudb.integration.runtime import Runtime
            from edudb.c_api import PlanNode, IndexSpec
            app = Runtime(sys.argv[1])
            ctx = app.context('committed')
            app.executor.execute(PlanNode('i','Insert',properties=dict(table_id='t',rows=[dict(id=1,v='committed')])),ctx)
            app.executor.create_index(IndexSpec('ix','t',['id'],True),ctx)
            app.transactions.commit(ctx.tx_id)
            ctx = app.context('loser')
            app.executor.execute(PlanNode('i2','Insert',properties=dict(table_id='t',rows=[dict(id=2,v='uncommitted')])),ctx)
            if sys.argv[2] == 'undo':
                original = app.pages.apply_image
                def crash_during_undo(key,image,lsn):
                    if app.wal.get(lsn)['kind'] == 'CLR':
                        os._exit(43)
                    return original(key,image,lsn)
                app.pages.apply_image = crash_during_undo
                app.transactions.rollback(ctx.tx_id)
            os._exit(42)
        ''')
        result = subprocess.run([sys.executable, "-c", script, str(self.root), mode],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 43 if mode == "undo" else 42, result.stderr)
        self.app = Runtime(self.root)
        self.assertEqual(self.query(), [[1, "committed"]])
        ctx = self.app.context()
        info = self.app.indexes.list_indexes("t", ctx)[0]
        self.assertEqual(len(list(self.app.indexes.search_index(info.id, RangeSpec(1, 1), ctx))), 1)
        self.assertEqual(list(self.app.indexes.search_index(info.id, RangeSpec(2, 2), ctx)), [])
        self.app.transactions.commit(ctx.tx_id)
        self.reopen()
        self.assertEqual(self.query(), [[1, "committed"]])

    def test_real_process_crash_recovers_data_and_index(self):
        self._child_crash("write")

    def test_crash_during_rollback_resumes_clr_undo(self):
        self._child_crash("undo")
