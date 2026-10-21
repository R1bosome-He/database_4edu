"""End-to-end tests: SQL text -> module B compiler -> module C executor/WAL.

These prove B produces plans that actually run on C, cross-checking results
against SQLite where useful, and exercising the optimizer-on/off equivalence
with real page-read metrics.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest

from edudb.c_api import DBError
from edudb.integration.runtime import Runtime
from edudb.sql import SqlSession


class SqlEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Runtime(self.tmp.name, buffer_pages=8)
        self.addCleanup(self.app.close)
        self.s = SqlSession(self.app)

    def students(self):
        self.s.execute("CREATE TABLE students (id INT PRIMARY KEY, name TEXT NOT NULL, score INT)")

    def test_create_insert_select_and_reopen(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        with Runtime(directory) as app:
            s = SqlSession(app)
            s.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
            s.execute("INSERT INTO t VALUES (1, 'Ada'), (2, 'Bob')")
        with Runtime(directory) as app:
            s = SqlSession(app)
            rows = s.execute("SELECT id, name FROM t ORDER BY id").rows
            self.assertEqual(rows, [[1, "Ada"], [2, "Bob"]])

    def test_where_projection_and_multi_key_order(self):
        self.students()
        self.s.execute("INSERT INTO students VALUES (1,'Ada',90),(2,'Bob',70),(3,'Cat',85),(4,'Dan',70)")
        rows = self.s.execute("SELECT name, score FROM students WHERE score >= 75 "
                              "ORDER BY score DESC, name ASC").rows
        self.assertEqual(rows, [["Ada", 90], ["Cat", 85]])
        rows = self.s.execute("SELECT name FROM students ORDER BY score, name").rows
        self.assertEqual(rows, [["Bob"], ["Dan"], ["Cat"], ["Ada"]])

    def test_join_matches_sqlite(self):
        self.students()
        self.s.execute("CREATE TABLE enroll (sid INT, course TEXT)")
        self.s.execute("INSERT INTO students VALUES (1,'Ada',90),(2,'Bob',70),(3,'Cat',85)")
        self.s.execute("INSERT INTO enroll VALUES (1,'db'),(1,'os'),(3,'db'),(9,'ai')")
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("create table students(id integer, name text, score integer)")
        db.execute("create table enroll(sid integer, course text)")
        db.executemany("insert into students values(?,?,?)", [(1, "Ada", 90), (2, "Bob", 70), (3, "Cat", 85)])
        db.executemany("insert into enroll values(?,?)", [(1, "db"), (1, "os"), (3, "db"), (9, "ai")])
        for kind in ["INNER", "LEFT", "RIGHT", "FULL"]:
            sql = f"SELECT students.name, enroll.course FROM students {kind} JOIN enroll ON students.id = enroll.sid"
            expected = db.execute(sql.lower()).fetchall()
            got = [tuple(r) for r in self.s.execute(sql).rows]
            self.assertCountEqual(got, expected, kind)

    def test_aggregate_matches_sqlite(self):
        self.s.execute("CREATE TABLE t (g TEXT, v INT)")
        data = [("a", 1), ("a", 1), ("a", None), ("b", 5), (None, None)]
        self.s.execute("INSERT INTO t VALUES " + ",".join(
            f"({'NULL' if g is None else repr(g)}, {'NULL' if v is None else v})" for g, v in data))
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("create table t(g text, v integer)")
        db.executemany("insert into t values(?,?)", data)
        sql = "SELECT g, COUNT(*), SUM(v), AVG(v), COUNT(DISTINCT v) FROM t GROUP BY g HAVING COUNT(*) > 1"
        expected = db.execute(sql.lower()).fetchall()
        got = [tuple(r) for r in self.s.execute(sql).rows]
        self.assertCountEqual(got, expected)

    def test_optimizer_equivalence_and_cold_cache_page_reads(self):
        self.students()
        payload = "n" * 180
        self.s.execute("INSERT INTO students VALUES " + ",".join(
            f"({i}, '{payload}', {i})" for i in range(80)))
        self.s.execute("CREATE INDEX idx_id ON students (id)")
        self.app.pages.reset_cache()
        off = self.s.execute("SELECT name FROM students WHERE id = 61", optimizer_enabled=False)
        self.app.pages.reset_cache()
        on = self.s.execute("SELECT name FROM students WHERE id = 61", optimizer_enabled=True)
        self.assertEqual(off.rows, on.rows)
        self.assertEqual(on.rows, [[payload]])
        self.assertGreater(off.metrics.data_page_reads, on.metrics.data_page_reads)
        self.assertGreater(on.metrics.index_page_reads, 0)

    def test_update_and_delete(self):
        self.students()
        self.s.execute("INSERT INTO students VALUES (1,'Ada',90),(2,'Bob',70),(3,'Cat',60)")
        self.assertEqual(self.s.execute("UPDATE students SET score = score + 5 WHERE score < 80").affected_rows, 2)
        self.assertEqual(self.s.execute("SELECT name, score FROM students ORDER BY id").rows,
                         [["Ada", 90], ["Bob", 75], ["Cat", 65]])
        self.assertEqual(self.s.execute("DELETE FROM students WHERE score < 80").affected_rows, 2)
        self.assertEqual(self.s.execute("SELECT name FROM students").rows, [["Ada"]])

    def test_transaction_rollback_restores(self):
        self.students()
        self.s.execute("INSERT INTO students VALUES (1,'Ada',90)")
        self.s.execute("BEGIN")
        self.s.execute("UPDATE students SET score = 0 WHERE id = 1")
        self.assertEqual(self.s.execute("SELECT score FROM students WHERE id = 1").rows, [[0]])
        self.s.execute("ROLLBACK")
        self.assertEqual(self.s.execute("SELECT score FROM students WHERE id = 1").rows, [[90]])

    def test_constraints_from_create_table(self):
        self.s.execute("CREATE TABLE parent (pid INT PRIMARY KEY, label TEXT)")
        self.s.execute("CREATE TABLE child (id INT PRIMARY KEY, parent INT REFERENCES parent(pid), "
                       "v INT CHECK (v >= 0), UNIQUE (v))")
        self.s.execute("INSERT INTO parent VALUES (1, 'p')")
        self.s.execute("INSERT INTO child VALUES (1, 1, 5)")
        for row in ["(2, 99, 6)", "(3, 1, -1)", "(4, 1, 5)"]:
            with self.assertRaises(DBError):
                self.s.execute(f"INSERT INTO child VALUES {row}")
        self.assertEqual(self.s.execute("SELECT id FROM child").rows, [[1]])

    def test_runtime_error_aborts_transaction(self):
        self.students()
        self.s.execute("INSERT INTO students VALUES (1, 'A', 1)")
        with self.assertRaises(DBError) as raised:
            self.s.execute("INSERT INTO students VALUES (1, 'B', 2)")
        self.assertEqual(raised.exception.transaction_effect, "TRANSACTION_ABORTED")
        self.assertEqual(self.s.execute("SELECT name FROM students").rows, [["A"]])

    def test_distinct_and_limit(self):
        self.students()
        self.s.execute("INSERT INTO students VALUES (1,'Ada',70),(2,'Bob',70),(3,'Cat',90),(4,'Dan',90)")
        self.assertEqual(self.s.execute("SELECT DISTINCT score FROM students ORDER BY score").rows, [[70], [90]])
        self.assertEqual(self.s.execute("SELECT name FROM students ORDER BY id LIMIT 2 OFFSET 1").rows,
                         [["Bob"], ["Cat"]])

    def test_explain_shows_physical_shape(self):
        self.students()
        self.s.execute("CREATE INDEX idx_id ON students (id)")
        text = "\n".join(r[0] for r in self.s.execute("EXPLAIN SELECT name FROM students WHERE id = 1").rows)
        self.assertIn("IndexScan", text)
        baseline = "\n".join(r[0] for r in self.s.execute(
            "EXPLAIN SELECT name FROM students WHERE id = 1", optimizer_enabled=False).rows)
        self.assertIn("SeqScan", baseline)


if __name__ == "__main__":
    unittest.main()
