"""Run B's end-to-end cases on real A pages and C WAL, not ReferencePageStore."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from edudb.c_api import DBError
from edudb.integration.contract_runtime import ContractRuntime
from edudb.module_a import StorageService
from edudb.sql import SqlSession
import test_sql_end_to_end as sql_tests


class IntegratedSqlTest(sql_tests.SqlEndToEndTest):
    def setUp(self):
        replacement = patch.object(sql_tests, "Runtime", ContractRuntime)
        replacement.start()
        self.addCleanup(replacement.stop)
        super().setUp()

    def test_uses_real_a_and_persistent_ids(self):
        self.students()
        self.assertIsInstance(self.app.storage, StorageService)
        table = self.app.storage.get_table("students")
        self.assertNotEqual(table.id, table.name)
        self.assertFalse((Path(self.tmp.name) / "c_pages_v1").exists())
        self.s.execute("INSERT INTO students VALUES (1,'Ada',90)")
        self.s.execute("CREATE INDEX ix ON students(id)")
        self.assertEqual(len(self.app.storage.get_table(table.id).indexes), 1)
        self.assertEqual(self.app.storage.diagnostics()["pinned_handles"], 0)

    def test_index_ddl_rejected_inside_business_transaction(self):
        self.students()
        self.s.execute("BEGIN")
        with self.assertRaises(DBError) as error:
            self.s.execute("CREATE INDEX ix ON students(id)")
        self.assertEqual(error.exception.code, "DDL_TRANSACTION_CONFLICT")
        self.s.execute("ROLLBACK")

    def test_self_referencing_foreign_key(self):
        self.s.execute("CREATE TABLE tree (id INT PRIMARY KEY, parent INT REFERENCES tree(id))")
        self.s.execute("INSERT INTO tree VALUES (1,1),(2,1)")
        with self.assertRaises(DBError):
            self.s.execute("INSERT INTO tree VALUES (3,99)")
        self.assertEqual(self.s.execute("SELECT COUNT(*) FROM tree").rows, [[2]])

    def test_cache_reset_refuses_active_transaction(self):
        self.s.execute("BEGIN")
        with self.assertRaises(DBError) as error:
            self.app.pages.reset_cache()
        self.assertEqual(error.exception.code, "EXPERIMENT_BUSY")
        self.s.execute("ROLLBACK")

    def test_global_writer_timeout_does_not_undo_other_transaction(self):
        self.s.execute("CREATE TABLE one (id INT)")
        self.s.execute("CREATE TABLE two (id INT)")
        self.s.execute("BEGIN")
        self.s.execute("INSERT INTO one VALUES (1)")
        other = SqlSession(self.app)
        with self.assertRaises(DBError) as error:
            other.execute("INSERT INTO two VALUES (2)")
        self.assertEqual(error.exception.code, "LOCK_TIMEOUT")
        self.s.execute("COMMIT")
        self.assertEqual(other.execute("SELECT id FROM one").rows, [[1]])
        self.assertEqual(other.execute("SELECT id FROM two").rows, [])

    def test_failed_unique_index_leaves_no_catalog_or_pins(self):
        self.students()
        self.s.execute("INSERT INTO students VALUES (1,'same',90),(2,'same',91)")
        with self.assertRaises(DBError):
            self.s.execute("CREATE UNIQUE INDEX ix ON students(name)")
        self.assertEqual(self.app.storage.get_table("students").indexes, [])
        self.assertFalse(self.app.transactions.has_active_transactions())
        self.assertEqual(self.app.storage.diagnostics()["pinned_handles"], 0)

    def test_index_split_delete_rollback_and_restart(self):
        self.students()
        self.s.execute("INSERT INTO students VALUES " + ",".join(f"({i},'name',90)" for i in range(40)))
        self.s.execute("CREATE INDEX ix ON students(id)")
        self.s.execute("BEGIN")
        self.s.execute("DELETE FROM students WHERE id < 35")
        self.s.execute("UPDATE students SET id = 101 WHERE id = 39")
        self.s.execute("ROLLBACK")
        self.app.close()
        with ContractRuntime(self.tmp.name) as reopened:
            session = SqlSession(reopened)
            self.assertEqual(session.execute("SELECT COUNT(*) FROM students").rows, [[40]])
            self.assertEqual(session.execute("SELECT id FROM students WHERE id = 39").rows, [[39]])
            self.assertEqual(session.execute("SELECT id FROM students WHERE id = 101").rows, [])


class CrashRecoveryTest(unittest.TestCase):
    def test_restart_after_clr_and_during_index_ddl(self):
        for stage in ("clr", "index"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                script = '''
import os, sys
from edudb.integration.contract_runtime import ContractRuntime
from edudb.sql import SqlSession
r = ContractRuntime(sys.argv[1])
s = SqlSession(r)
s.execute("CREATE TABLE t (id INT PRIMARY KEY, value TEXT)")
s.execute("INSERT INTO t VALUES (1,'old')")
if sys.argv[2] == "index":
    original = r.pages.publish_for
    def interrupted(tx, prepared):
        result = original(tx, prepared)
        r.storage.flush_all()
        os._exit(0)
    r.pages.publish_for = interrupted
    s.execute("CREATE INDEX ix ON t(id)")
else:
    s.execute("CREATE INDEX ix ON t(id)")
    s.execute("BEGIN")
    s.execute("UPDATE t SET id=2, value='new' WHERE id=1")
    r.storage.flush_all()
    original = r.pages.apply_image
    def interrupted(key, image, lsn):
        original(key, image, lsn)
        if r.wal.get(lsn)["kind"] == "CLR":
            os._exit(0)
    r.pages.apply_image = interrupted
    s.execute("ROLLBACK")
os._exit(99)
'''
                result = subprocess.run([sys.executable, "-c", script, directory, stage],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                for _ in range(2):
                    with ContractRuntime(directory) as runtime:
                        session = SqlSession(runtime)
                        self.assertEqual(session.execute("SELECT value FROM t WHERE id=1").rows, [["old"]])
                        self.assertEqual(session.execute("SELECT value FROM t WHERE id=2").rows, [])
                        self.assertEqual(len(runtime.storage.get_table("t").indexes), 0 if stage == "index" else 1)

    def test_missing_wal_is_not_silently_recreated(self):
        with tempfile.TemporaryDirectory() as directory:
            with ContractRuntime(directory) as runtime:
                SqlSession(runtime).execute("CREATE TABLE t (id INT)")
            (Path(directory) / "wal" / "contract.wal").unlink()
            with self.assertRaises(DBError) as error:
                ContractRuntime(directory)
            self.assertEqual(error.exception.code, "WAL_NOT_FOUND")

    def test_shell_persists_and_reopens(self):
        with tempfile.TemporaryDirectory() as directory:
            base = [sys.executable, "-m", "edudb.integrated", "--db", directory]
            result = subprocess.run(base + ["--sql", "CREATE TABLE t (id INT)", "--sql", "INSERT INTO t VALUES (7)"],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(base + ["--sql", "SELECT id FROM t"], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["rows"], [[7]])

    def test_uncommitted_flushed_table_and_index_undo_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            script = '''
import os, sys
from edudb.integration.contract_runtime import ContractRuntime
from edudb.sql import SqlSession
r = ContractRuntime(sys.argv[1])
s = SqlSession(r)
s.execute("CREATE TABLE t (id INT PRIMARY KEY, value TEXT)")
s.execute("INSERT INTO t VALUES (1, 'committed')")
s.execute("CREATE INDEX ix ON t(id)")
s.execute("BEGIN")
s.execute("UPDATE t SET id=2, value='uncommitted' WHERE id=1")
r.storage.flush_all()
os._exit(0)
'''
            result = subprocess.run([sys.executable, "-c", script, directory], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            for _ in range(2):
                with ContractRuntime(directory) as runtime:
                    session = SqlSession(runtime)
                    self.assertEqual(session.execute("SELECT value FROM t WHERE id=1").rows, [["committed"]])
                    self.assertEqual(session.execute("SELECT value FROM t WHERE id=2").rows, [])
                    self.assertEqual(runtime.storage.diagnostics()["pinned_handles"], 0)


if __name__ == "__main__":
    unittest.main()
