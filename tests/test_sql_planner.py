"""Binding + planning + optimizer tests using an in-memory catalog (no runtime)."""
from __future__ import annotations

import unittest

from edudb.c_api import DBError
from edudb.sql import CompileOptions, InMemoryCatalog, compile_sql
from edudb.sql.catalog import ColumnInfo, IndexDesc, TableInfo


def find(node, op):
    if node.op == op:
        return node
    for child in node.children:
        found = find(child, op)
        if found is not None:
            return found
    return None


def all_ops(node):
    ops = [node.op]
    for child in node.children:
        ops += all_ops(child)
    return ops


class SqlPlannerTest(unittest.TestCase):
    def setUp(self):
        self.cat = InMemoryCatalog()
        self.cat.add_table(TableInfo("students", [ColumnInfo("id", "INT", False),
                                                  ColumnInfo("name", "TEXT"), ColumnInfo("score", "INT")], "id"))
        self.cat.add_index("students", IndexDesc("ci_id", "idx_id", "id", True))
        self.cat.add_index("students", IndexDesc("ci_score", "idx_score", "score", False))
        self.cat.add_table(TableInfo("a", [ColumnInfo("id", "INT"), ColumnInfo("x", "INT")]))
        self.cat.add_table(TableInfo("b", [ColumnInfo("id", "INT"), ColumnInfo("y", "INT")]))

    def plan(self, sql, optimizer=True):
        return compile_sql(sql, self.cat, CompileOptions(optimizer)).plan

    def assertRaisesCode(self, code, sql):
        with self.assertRaises(DBError) as raised:
            compile_sql(sql, self.cat)
        self.assertEqual(raised.exception.code, code)

    # --- catalog binding ------------------------------------------------
    def test_unknown_table_and_column(self):
        self.assertRaisesCode("UNKNOWN_TABLE", "SELECT * FROM missing")
        self.assertRaisesCode("UNKNOWN_COLUMN", "SELECT nope FROM students")
        self.assertRaisesCode("UNKNOWN_COLUMN", "SELECT * FROM students WHERE nope = 1")

    def test_ambiguous_column(self):
        self.assertRaisesCode("AMBIGUOUS_COLUMN", "SELECT id FROM a JOIN b ON a.id = b.id")
        # Qualifying resolves the ambiguity.
        self.assertIsNotNone(self.plan("SELECT a.id FROM a JOIN b ON a.id = b.id"))

    def test_type_rules(self):
        self.assertRaisesCode("TYPE_ERROR", "SELECT * FROM students WHERE name = 5")
        self.assertRaisesCode("TYPE_ERROR", "SELECT * FROM students WHERE score + name > 1")
        self.assertRaisesCode("TYPE_ERROR", "SELECT * FROM students WHERE score")
        self.assertRaisesCode("TYPE_ERROR", "UPDATE students SET name = 1 WHERE id = 1")

    # --- index selection ------------------------------------------------
    def test_equality_uses_index_when_enabled(self):
        optimized = self.plan("SELECT name FROM students WHERE id = 5", optimizer=True)
        self.assertIsNotNone(find(optimized, "IndexScan"))
        self.assertIsNone(find(optimized, "SeqScan"))
        baseline = self.plan("SELECT name FROM students WHERE id = 5", optimizer=False)
        self.assertIsNone(find(baseline, "IndexScan"))
        self.assertIsNotNone(find(baseline, "Filter"))

    def test_range_predicate_uses_index_with_bounds(self):
        node = find(self.plan("SELECT name FROM students WHERE score >= 70 AND score <= 90"), "IndexScan")
        self.assertIsNotNone(node)
        spec = node.properties["range"]
        self.assertEqual((spec.lower, spec.upper, spec.lower_inclusive, spec.upper_inclusive), (70, 90, True, True))

    def test_unindexed_column_stays_seqscan(self):
        node = self.plan("SELECT id FROM students WHERE name = 'x'")
        self.assertIsNone(find(node, "IndexScan"))
        self.assertIsNotNone(find(node, "SeqScan"))

    # --- predicate pushdown ---------------------------------------------
    def test_pushdown_to_preserved_side_only(self):
        # Filter on the right (null-supplying) side of a LEFT JOIN must stay above the join.
        node = self.plan("SELECT * FROM a LEFT JOIN b ON a.id = b.id WHERE b.y = 5")
        self.assertEqual(node.op if node.op != "Projection" else node.children[0].op, "Filter")
        join = find(node, "HashJoin") or find(node, "NestedLoopJoin")
        self.assertEqual(join.children[1].op, "SeqScan")   # b not pre-filtered
        # Filter on the preserved (left) side is pushed down into that scan.
        node2 = self.plan("SELECT * FROM a LEFT JOIN b ON a.id = b.id WHERE a.x = 5")
        join2 = find(node2, "HashJoin") or find(node2, "NestedLoopJoin")
        self.assertEqual(join2.children[0].op, "Filter")
        self.assertEqual(join2.children[0].children[0].op, "SeqScan")

    def test_inner_join_pushes_both_sides(self):
        node = self.plan("SELECT * FROM a JOIN b ON a.id = b.id WHERE a.x = 1 AND b.y = 2")
        join = find(node, "HashJoin")
        self.assertIsNotNone(join)
        self.assertEqual(join.children[0].op, "Filter")
        self.assertEqual(join.children[1].op, "Filter")

    # --- hash join ------------------------------------------------------
    def test_equi_join_becomes_hash_join(self):
        node = self.plan("SELECT * FROM a JOIN b ON a.id = b.id")
        hashjoin = find(node, "HashJoin")
        self.assertIsNotNone(hashjoin)
        self.assertEqual(hashjoin.properties["left_key"].slot_id, "a.id")
        self.assertEqual(hashjoin.properties["right_key"].slot_id, "b.id")
        # Without the optimizer it stays a nested-loop join.
        self.assertIsNotNone(find(self.plan("SELECT * FROM a JOIN b ON a.id = b.id", False), "NestedLoopJoin"))

    def test_non_equi_join_stays_nested_loop(self):
        node = self.plan("SELECT * FROM a JOIN b ON a.x > b.y")
        self.assertIsNone(find(node, "HashJoin"))
        self.assertIsNotNone(find(node, "NestedLoopJoin"))

    # --- aggregation ----------------------------------------------------
    def test_grouping_requires_grouped_columns(self):
        self.assertRaisesCode("GROUPING_ERROR", "SELECT name FROM students GROUP BY score")
        self.assertRaisesCode("GROUPING_ERROR", "SELECT COUNT(*) FROM students WHERE SUM(score) > 1")
        self.assertIsNotNone(find(self.plan("SELECT score, COUNT(*) FROM students GROUP BY score"), "Aggregate"))

    def test_having_becomes_filter_above_aggregate(self):
        node = self.plan("SELECT score, COUNT(*) FROM students GROUP BY score HAVING COUNT(*) > 1")
        having = find(node, "Filter")
        self.assertEqual(having.children[0].op, "Aggregate")

    def test_order_by_ordinal_out_of_range(self):
        self.assertRaisesCode("INVALID_ORDER", "SELECT id FROM students ORDER BY 5")
        self.assertRaisesCode("INVALID_ORDER", "SELECT DISTINCT name FROM students ORDER BY score")

    # --- DDL binding ----------------------------------------------------
    def test_create_table_rejects_duplicate_and_composite_pk(self):
        self.assertRaisesCode("TABLE_EXISTS", "CREATE TABLE students (id INT)")
        self.assertRaisesCode("INVALID_CONSTRAINT",
                              "CREATE TABLE t (a INT, b INT, PRIMARY KEY (a, b))")

    def test_create_index_validates_column(self):
        self.assertRaisesCode("UNKNOWN_COLUMN", "CREATE INDEX ix ON students (nope)")
        compiled = compile_sql("CREATE UNIQUE INDEX ix ON students (name)", self.cat)
        self.assertEqual(compiled.index_spec.column_ids, ["name"])
        self.assertTrue(compiled.index_spec.unique)

    def test_privileges_are_reported(self):
        compiled = compile_sql("SELECT * FROM a JOIN b ON a.id = b.id", self.cat)
        tables = {(p.object_id, p.action) for p in compiled.required_privileges}
        self.assertEqual(tables, {("a", "SELECT"), ("b", "SELECT")})


if __name__ == "__main__":
    unittest.main()
