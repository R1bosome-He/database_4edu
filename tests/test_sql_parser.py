"""Pure parser/tokenizer tests for module B: no catalog, no runtime."""
from __future__ import annotations

import unittest

from edudb.c_api import DBError
from edudb.sql import ast, parse


class SqlParserTest(unittest.TestCase):
    def test_string_literal_is_opaque_to_keywords_and_commas(self):
        stmt = parse("SELECT * FROM t WHERE name = 'a, and ''quoted'' where from'")
        self.assertIsInstance(stmt, ast.SelectStmt)
        condition = stmt.where
        self.assertIsInstance(condition, ast.BinaryOp)
        self.assertEqual(condition.op, "=")
        self.assertEqual(condition.right.value, "a, and 'quoted' where from")

    def test_line_and_block_comments_are_ignored(self):
        stmt = parse("SELECT a, -- trailing\n /* block , AND */ b FROM t")
        self.assertEqual(len(stmt.select_list), 2)
        self.assertEqual(stmt.select_list[0].expr.name, "a")
        self.assertEqual(stmt.select_list[1].expr.name, "b")

    def test_arithmetic_precedence(self):
        stmt = parse("SELECT 1 + 2 * 3")
        expr = stmt.select_list[0].expr
        self.assertEqual(expr.op, "+")
        self.assertIsInstance(expr.right, ast.BinaryOp)
        self.assertEqual(expr.right.op, "*")

    def test_boolean_precedence_and_not(self):
        stmt = parse("SELECT * FROM t WHERE a = 1 AND b = 2 OR c = 3")
        self.assertEqual(stmt.where.op, "OR")       # OR binds loosest
        self.assertEqual(stmt.where.left.op, "AND")
        not_stmt = parse("SELECT * FROM t WHERE NOT a = 1")
        self.assertIsInstance(not_stmt.where, ast.UnaryOp)
        self.assertEqual(not_stmt.where.op, "NOT")
        self.assertIsInstance(not_stmt.where.operand, ast.BinaryOp)

    def test_is_null_in_between(self):
        stmt = parse("SELECT * FROM t WHERE a IS NOT NULL AND b IN (1, 2, 3) AND c NOT BETWEEN 4 AND 5")
        top = stmt.where
        self.assertEqual(top.op, "AND")
        self.assertIsInstance(top.left.left, ast.IsNull)
        self.assertTrue(top.left.left.negated)
        in_node = top.left.right
        self.assertIsInstance(in_node, ast.InList)
        self.assertEqual(len(in_node.items), 3)
        between = top.right
        self.assertIsInstance(between, ast.Between)
        self.assertTrue(between.negated)

    def test_aggregate_calls_and_star(self):
        stmt = parse("SELECT COUNT(*), COUNT(DISTINCT x), SUM(y) AS total FROM t")
        count_star, count_distinct, total = stmt.select_list
        self.assertTrue(count_star.expr.star)
        self.assertTrue(count_distinct.expr.distinct)
        self.assertEqual(total.alias, "total")

    def test_join_and_order_limit(self):
        stmt = parse("SELECT a.x FROM a LEFT OUTER JOIN b ON a.id = b.id "
                     "ORDER BY a.x DESC NULLS FIRST LIMIT 10 OFFSET 5")
        self.assertIsInstance(stmt.from_, ast.JoinRef)
        self.assertEqual(stmt.from_.join_type, "LEFT")
        self.assertEqual(stmt.limit, 10)
        self.assertEqual(stmt.offset, 5)
        self.assertTrue(stmt.order_by[0].descending)
        self.assertTrue(stmt.order_by[0].nulls_first)

    def test_create_table_constraints(self):
        stmt = parse("CREATE TABLE t (id INT PRIMARY KEY, p INT REFERENCES parent(pid), "
                     "v INT CHECK (v >= 0), UNIQUE (v))")
        self.assertIsInstance(stmt, ast.CreateTableStmt)
        self.assertEqual(stmt.columns[0].name, "id")
        self.assertTrue(stmt.columns[0].primary_key)
        kinds = sorted(c.kind for c in stmt.constraints)
        self.assertEqual(kinds, ["CHECK", "FOREIGN_KEY", "UNIQUE"])

    def test_insert_update_delete_and_explain(self):
        self.assertIsInstance(parse("INSERT INTO t (a, b) VALUES (1, 2), (3, 4)"), ast.InsertStmt)
        self.assertIsInstance(parse("UPDATE t SET a = a + 1 WHERE b = 2"), ast.UpdateStmt)
        self.assertIsInstance(parse("DELETE FROM t WHERE a = 1"), ast.DeleteStmt)
        self.assertIsInstance(parse("EXPLAIN SELECT * FROM t"), ast.ExplainStmt)

    def test_transaction_statements(self):
        for sql, action in [("BEGIN", "BEGIN"), ("START TRANSACTION", "BEGIN"),
                            ("COMMIT", "COMMIT"), ("ROLLBACK WORK", "ROLLBACK")]:
            self.assertEqual(parse(sql).action, action)

    def test_errors_carry_position(self):
        for sql in ["SELECT FROM t", "SELECT * FROM", "SELECT * FROM t WHERE",
                    "SELECT * FROM t t2 t3 extra", "DROP TABLE t", "SELECT 1; SELECT 2"]:
            with self.assertRaises(DBError) as raised:
                parse(sql)
            self.assertEqual(raised.exception.code, "SYNTAX_ERROR")
            self.assertIsInstance(raised.exception.position, int)

    def test_insert_select_is_rejected_clearly(self):
        with self.assertRaises(DBError) as raised:
            parse("INSERT INTO t SELECT * FROM s")
        self.assertEqual(raised.exception.code, "SYNTAX_ERROR")

    def test_unterminated_string(self):
        with self.assertRaises(DBError):
            parse("SELECT 'oops")


if __name__ == "__main__":
    unittest.main()
