from __future__ import annotations

import tempfile
import unittest

from edu_dbms.database import Database
from edu_dbms.web import serialize_result


class EduDBMSTest(unittest.TestCase):
    def test_index_optimizer_reduces_page_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(tmp, buffer_pages=4)
            db.execute("CREATE TABLE students (id INT, name TEXT, PRIMARY KEY (id));")
            for i in range(300):
                db.execute(f"INSERT INTO students (id, name) VALUES ({i}, 's{i}');")
            db.execute("CREATE INDEX idx_students_id ON students (id);")

            result = db.execute("COMPARE SELECT id, name FROM students WHERE id = 250;")
            off, on = result.comparisons

            self.assertEqual(off.rows, on.rows)
            self.assertEqual(on.rows[0]["id"], 250)
            self.assertIn("SeqScan", off.plan)
            self.assertIn("IndexScan", on.plan)
            self.assertLess(on.metrics["disk_reads"], off.metrics["disk_reads"])

    def test_permissions_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(tmp)
            db.execute("CREATE TABLE t (id INT, name TEXT, PRIMARY KEY (id));")
            db.execute("INSERT INTO t (id, name) VALUES (1, 'one');")
            db.execute("CREATE USER reader PASSWORD 'pw';")

            with self.assertRaises(PermissionError):
                db.execute("SELECT * FROM t WHERE id = 1;", user="reader")

            db.execute("GRANT SELECT ON t TO reader;")
            result = db.execute("SELECT * FROM t WHERE id = 1;", user="reader")
            self.assertEqual(result.rows[0]["name"], "one")

    def test_join_hash_plan_matches_nested_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(tmp, buffer_pages=4)
            db.execute("CREATE TABLE students (id INT, name TEXT, PRIMARY KEY (id));")
            db.execute("CREATE TABLE scores (student_id INT, score FLOAT);")
            for i in range(1, 20):
                db.execute(f"INSERT INTO students (id, name) VALUES ({i}, 's{i}');")
                db.execute(f"INSERT INTO scores (student_id, score) VALUES ({i}, {80 + i});")
            db.execute("CREATE INDEX idx_students_id ON students (id);")

            result = db.execute(
                "COMPARE SELECT students.name, scores.score "
                "FROM students JOIN scores ON students.id = scores.student_id "
                "WHERE students.id = 7;"
            )
            off, on = result.comparisons
            self.assertEqual(off.rows, on.rows)
            self.assertIn("NestedLoopJoin", off.plan)
            self.assertIn("HashJoin", on.plan)

    def test_web_result_serialization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(tmp)
            db.execute("CREATE TABLE t (id INT, name TEXT, PRIMARY KEY (id));")
            db.execute("INSERT INTO t (id, name) VALUES (1, 'one');")
            result = db.execute("SELECT * FROM t WHERE id = 1;")
            payload = serialize_result(result)
            self.assertEqual(payload["kind"], "table")
            self.assertEqual(payload["rows"][0]["name"], "one")
            self.assertTrue(payload["plan_tree"])

    def test_update_delete_order_by_and_transactions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(tmp)
            db.execute("CREATE TABLE t (id INT, name TEXT, score INT, PRIMARY KEY (id));")
            db.execute("INSERT INTO t (id, name, score) VALUES (1, 'one', 70);")
            db.execute("INSERT INTO t (id, name, score) VALUES (2, 'two', 90);")
            db.execute("INSERT INTO t (id, name, score) VALUES (3, 'three', 90);")
            db.execute("CREATE INDEX idx_t_id ON t (id);")

            ordered = db.execute("SELECT id, score FROM t ORDER BY score DESC, id ASC;")
            self.assertEqual([row["id"] for row in ordered.rows], [2, 3, 1])
            self.assertIn("Sort", ordered.plan)

            db.execute("UPDATE t SET score = 99 WHERE id = 1;")
            updated = db.execute("SELECT id, score FROM t WHERE id = 1;")
            self.assertEqual(updated.rows[0]["score"], 99)
            self.assertIn("IndexScan", updated.plan)

            db.execute("BEGIN;")
            db.execute("UPDATE t SET name = 'rolled_back' WHERE id = 1;")
            db.execute("ROLLBACK;")
            rolled_back = db.execute("SELECT id, name FROM t WHERE id = 1;")
            self.assertEqual(rolled_back.rows[0]["name"], "one")

            db.execute("BEGIN;")
            db.execute("DELETE FROM t WHERE score < 95;")
            db.execute("COMMIT;")
            remaining = db.execute("SELECT id FROM t ORDER BY id ASC;")
            self.assertEqual([row["id"] for row in remaining.rows], [1])


if __name__ == "__main__":
    unittest.main()
