from __future__ import annotations

import itertools
import sqlite3
import tempfile
import unittest
from dataclasses import asdict

from edudb.c_api import (Constraint, DBError, Expr, IndexSpec, PlanNode, RangeSpec,
                        ResultColumn, plan_from_dict)
from edudb.integration.runtime import Runtime
from edudb.models import Column, TableSchema


def lit(value):
    return Expr("Literal", value)


def col(slot, level=0):
    return Expr("ColumnRef", slot_id=slot, level=level)


def binary(op, left, right):
    return Expr("Binary", operator=op, args=[left, right])


def values(node_id, columns, rows):
    return PlanNode(node_id, "Values", output_columns=columns, properties={"rows": rows})


class CExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Runtime(self.temp.name, buffer_pages=2)
        self.app.create_table(TableSchema("students", (Column("id", "INT", False), Column("name", "TEXT"),
                                                       Column("score", "INT")), "id"))
        self.columns = [ResultColumn("s.id", "id", "INT", False), ResultColumn("s.name", "name", "TEXT"),
                        ResultColumn("s.score", "score", "INT")]

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def ctx(self):
        return self.app.context()

    def insert(self, rows, ctx=None):
        own = ctx is None
        ctx = ctx or self.ctx()
        plan = PlanNode("ins", "Insert", properties={"table_id": "students", "rows": rows})
        result = self.app.executor.execute(plan, ctx)
        if own:
            self.app.transactions.commit(ctx.tx_id)
        return result

    def scan(self):
        return PlanNode("scan", "SeqScan", output_columns=self.columns, properties={"table_id": "students"})

    def run_plan(self, plan):
        ctx = self.ctx()
        result = self.app.executor.execute(plan, ctx)
        self.app.transactions.commit(ctx.tx_id)
        self.assertEqual(ctx.memory_used_bytes, 0)
        return result

    def test_filter_projection_sort_nulls_and_empty_columns(self):
        self.insert([dict(id=1, name="Ada", score=80), dict(id=2, name="Bob", score=None), dict(id=3, name="Cat", score=90)])
        filtered = PlanNode("f", "Filter", [self.scan()], self.columns, [binary(">=", col("s.score"), lit(80))])
        sorted_plan = PlanNode("sort", "Sort", [filtered], self.columns,
                               properties={"order_by": [dict(expr=col("s.score"), descending=True)]})
        plan = PlanNode("p", "Projection", [sorted_plan], [self.columns[1]], [col("s.name")])
        self.assertEqual(self.run_plan(plan).rows, [["Cat"], ["Ada"]])
        empty = PlanNode("empty", "Filter", [self.scan()], self.columns, [lit(False)])
        result = self.run_plan(empty)
        self.assertEqual(result.rows, [])
        self.assertEqual(result.columns, self.columns)

    def test_three_valued_boolean_logic(self):
        ctx = self.ctx()
        for a, b in itertools.product([True, False, None], repeat=2):
            expected_and = False if False in (a, b) else None if None in (a, b) else True
            expected_or = True if True in (a, b) else None if None in (a, b) else False
            self.assertIs(self.app.executor.evaluate(binary("AND", lit(a), lit(b)), {}, ctx), expected_and)
            self.assertIs(self.app.executor.evaluate(binary("OR", lit(a), lit(b)), {}, ctx), expected_or)
        self.assertIsNone(self.app.executor.evaluate(binary("=", lit(None), lit(None)), {}, ctx))
        self.assertTrue(self.app.executor.evaluate(Expr("IsNull", args=[lit(None)]), {}, ctx))
        with self.assertRaises(DBError):
            self.app.executor.evaluate(binary("=", lit(True), lit(1)), {}, ctx)
        self.app.transactions.commit(ctx.tx_id)

    def test_all_outer_joins_and_hash_match_sqlite(self):
        a = [ResultColumn("a.k", "k", "INT")]
        b = [ResultColumn("b.k", "k", "INT")]
        left_data, right_data = [[1], [1], [2], [None]], [[1], [1], [3], [None]]
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("create table a(k integer)")
        db.execute("create table b(k integer)")
        db.executemany("insert into a values (?)", left_data)
        db.executemany("insert into b values (?)", right_data)
        for kind in ["INNER", "LEFT", "RIGHT", "FULL"]:
            expected = db.execute(f"select a.k,b.k from a {kind} join b on a.k=b.k").fetchall()
            for algorithm in ["NestedLoopJoin", "HashJoin"]:
                with self.subTest(kind=kind, algorithm=algorithm):
                    plan = PlanNode("join", algorithm, [values("a", a, left_data), values("b", b, right_data)], a + b,
                                    [binary("=", col("a.k"), col("b.k"))],
                                    dict(join_type=kind, left_key=col("a.k"), right_key=col("b.k")))
                    self.assertCountEqual([tuple(r) for r in self.run_plan(plan).rows], expected)

    def test_empty_join_inputs(self):
        a, b = [ResultColumn("a", "a", "INT")], [ResultColumn("b", "b", "INT")]
        for algorithm in ["NestedLoopJoin", "HashJoin"]:
            plan = PlanNode("j", algorithm, [values("a", a, []), values("b", b, [[4]])], a+b,
                            [binary("=", col("a"), col("b"))],
                            dict(join_type="FULL", left_key=col("a"), right_key=col("b")))
            self.assertEqual(self.run_plan(plan).rows, [[None, 4]])

    def test_aggregate_having_and_distinct_match_sqlite(self):
        columns = [ResultColumn("g", "g", "TEXT"), ResultColumn("v", "v", "INT")]
        data = [["a", 1], ["a", 1], ["a", None], ["b", 5], [None, None]]
        outputs = [ResultColumn("group", "g", "TEXT"), ResultColumn("count", "n", "INT"),
                   ResultColumn("avg", "avg", "FLOAT"), ResultColumn("uniq", "u", "INT")]
        expressions = [col("g"), Expr("Aggregate", operator="COUNT"), Expr("Aggregate", operator="AVG", args=[col("v")]),
                       Expr("Aggregate", operator="COUNT", args=[col("v")], distinct=True)]
        group = PlanNode("agg", "Aggregate", [values("v", columns, data)], outputs, expressions,
                         dict(group_by=[col("g")]))
        plan = PlanNode("having", "Filter", [group], outputs, [binary(">", col("count"), lit(1))])
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("create table t(g text,v integer)")
        db.executemany("insert into t values (?,?)", data)
        expected = db.execute("select g,count(*),avg(v),count(distinct v) from t group by g having count(*)>1").fetchall()
        self.assertCountEqual([tuple(r) for r in self.run_plan(plan).rows], expected)

    def test_aggregate_empty_input(self):
        columns = [ResultColumn("v", "v", "INT")]
        outputs = [ResultColumn("n", "n", "INT"), ResultColumn("s", "s", "INT")]
        plan = PlanNode("g", "Aggregate", [values("v", columns, [])], outputs,
                        [Expr("Aggregate", operator="COUNT"), Expr("Aggregate", operator="SUM", args=[col("v")])])
        self.assertEqual(self.run_plan(plan).rows, [[0, None]])
        plan.properties["group_by"] = [col("v")]
        self.assertEqual(self.run_plan(plan).rows, [])

    def test_distinct_and_limit(self):
        columns = [ResultColumn("v", "v", "INT")]
        distinct = PlanNode("d", "Distinct", [values("v", columns, [[None], [1], [1], [None], [2]])], columns)
        limit = PlanNode("l", "Limit", [distinct], columns, properties=dict(limit=1, offset=1))
        self.assertEqual(self.run_plan(limit).rows, [[1]])
        limit.properties["limit"] = 0
        self.assertEqual(self.run_plan(limit).rows, [])

    def test_correlated_exists_and_scalar_subquery(self):
        a, b = [ResultColumn("a", "a", "INT")], [ResultColumn("b", "b", "INT")]
        sub = PlanNode("sf", "Filter", [values("sv", b, [[2], [3]])], b,
                       [binary("=", col("b"), col("a", 1))])
        outer = PlanNode("f", "Filter", [values("v", a, [[1], [2]])], a,
                         [Expr("Subquery", operator="EXISTS", plan=sub)])
        self.assertEqual(self.run_plan(outer).rows, [[2]])
        projection = PlanNode("p", "Projection", [values("v", a, [[1], [2]])], b,
                              [Expr("Subquery", operator="SCALAR", plan=sub)])
        self.assertEqual(self.run_plan(projection).rows, [[None], [2]])

    def test_in_not_in_null_and_empty(self):
        c = [ResultColumn("x", "x", "INT")]
        ctx = self.ctx()
        for mode, key, rows, expected in [("IN", 2, [[1], [None]], None), ("NOT IN", 2, [[1], [None]], None),
                                           ("IN", None, [], False), ("NOT IN", None, [], True),
                                           ("IN", 1, [[1], [None]], True)]:
            expr = Expr("Subquery", operator=mode, args=[lit(key)], plan=values("sub", c, rows))
            self.assertIs(self.app.executor.evaluate(expr, {}, ctx), expected)
        self.app.transactions.commit(ctx.tx_id)

    def test_scalar_cardinality_aborts_transaction(self):
        c = [ResultColumn("x", "x", "INT")]
        plan = PlanNode("p", "Projection", [values("one", [], [[]])], c,
                        [Expr("Subquery", plan=values("sub", c, [[1], [2]]))])
        ctx = self.ctx()
        with self.assertRaises(DBError) as raised:
            self.app.executor.execute(plan, ctx)
        self.assertEqual(raised.exception.code, "SUBQUERY_CARDINALITY")
        self.assertEqual(raised.exception.transaction_effect, "TRANSACTION_ABORTED")

    def test_operator_batches_and_close(self):
        c = [ResultColumn("x", "x", "INT")]
        ctx = self.ctx()
        operator = self.app.executor.open_operator(values("v", c, [[1], [2], [3]]), ctx)
        self.assertEqual(operator.next_batch(2), [[1], [2]])
        self.assertEqual(operator.next_batch(2), [[3]])
        self.assertEqual(operator.next_batch(2), [])
        operator.close()
        operator.close()
        self.assertEqual(operator.next_batch(), [])
        self.app.transactions.commit(ctx.tx_id)

    def test_memory_limit_and_cancellation(self):
        c = [ResultColumn("x", "x", "TEXT")]
        ctx = self.app.context(memory_limit_bytes=128)
        plan = PlanNode("sort", "Sort", [values("v", c, [["x" * 1000]])], c,
                        properties=dict(order_by=[dict(expr=col("x"))]))
        with self.assertRaises(DBError) as raised:
            self.app.executor.execute(plan, ctx)
        self.assertEqual(raised.exception.code, "RESOURCE_LIMIT")
        self.assertEqual(ctx.memory_used_bytes, 0)
        ctx = self.ctx()
        ctx.cancelled = True
        with self.assertRaises(DBError) as raised:
            self.app.executor.execute(values("v", c, [["a"]]), ctx)
        self.assertEqual(raised.exception.code, "CANCELLED")

    def test_plan_json_round_trip(self):
        plan = PlanNode("p", "Projection", [self.scan()], [self.columns[0]], [col("s.id")])
        restored = plan_from_dict(asdict(plan))
        self.assertEqual(plan, restored)
        self.assertEqual(self.run_plan(restored).rows, [])

    def test_arithmetic_errors_abort_and_short_circuit_works(self):
        ctx = self.ctx()
        bad = binary("/", lit(1), lit(0))
        self.assertIs(self.app.executor.evaluate(binary("AND", lit(False), bad), {}, ctx), False)
        self.app.transactions.commit(ctx.tx_id)
        for expression, code in [(bad, "DIVISION_BY_ZERO"),
                                  (binary("+", lit(2**63-1), lit(1)), "NUMERIC_OVERFLOW")]:
            ctx = self.ctx()
            plan = PlanNode("p", "Projection", [values("v", [], [[]])],
                            [ResultColumn("n", "n", "INT")], [expression])
            with self.assertRaises(DBError) as raised:
                self.app.executor.execute(plan, ctx)
            self.assertEqual(raised.exception.code, code)

    def test_open_operator_blocks_concurrent_statement_and_commit(self):
        ctx = self.ctx()
        operator = self.app.executor.open_operator(values("v", [ResultColumn("x", "x", "INT")], [[1], [2]]), ctx)
        operator.open()
        with self.assertRaises(DBError) as raised:
            self.app.executor.execute(self.scan(), ctx)
        self.assertEqual(raised.exception.code, "TRANSACTION_BUSY")
        with self.assertRaises(DBError):
            self.app.transactions.commit(ctx.tx_id)
        operator.close()
        self.app.transactions.commit(ctx.tx_id)

    def test_min_max_sum_and_count_column(self):
        c = [ResultColumn("v", "v", "INT")]
        outputs = [ResultColumn(name, name, "INT") for name in ["min", "max", "sum", "count"]]
        plan = PlanNode("g", "Aggregate", [values("v", c, [[None], [5], [2], [5]])], outputs,
                        [Expr("Aggregate", operator=name, args=[col("v")]) for name in ["MIN", "MAX", "SUM", "COUNT"]])
        self.assertEqual(self.run_plan(plan).rows, [[2, 5, 12, 3]])

    def test_update_assignments_use_old_row_and_not_null(self):
        self.insert([dict(id=1, name="a", score=10)])
        update = PlanNode("u", "Update", properties=dict(table_id="students", input_columns=self.columns,
            assignments={"id": col("s.score"), "score": col("s.id")}))
        self.run_plan(update)
        self.assertEqual(self.run_plan(self.scan()).rows, [[10, "a", 1]])
        with self.assertRaises(DBError):
            self.insert([dict(id=None, name="bad", score=1)])
        self.assertEqual(self.run_plan(self.scan()).rows, [[10, "a", 1]])

    def test_update_delete_and_index_rollback(self):
        self.insert([dict(id=i, name=str(i), score=i*10) for i in range(1, 6)])
        ctx = self.ctx()
        info = self.app.executor.create_index(IndexSpec("idx_id", "students", ["id"], True), ctx)
        self.app.transactions.commit(ctx.tx_id)
        ctx = self.ctx()
        plan = PlanNode("u", "Update", expressions=[binary("=", col("s.id"), lit(2))], properties=dict(
            table_id="students", input_columns=self.columns, assignments={"id": lit(9), "score": binary("+", col("s.score"), lit(1))}))
        self.assertEqual(self.app.executor.execute(plan, ctx).affected_rows, 1)
        self.assertEqual(len(list(self.app.indexes.search_index(info.id, RangeSpec(9, 9), ctx))), 1)
        self.app.transactions.rollback(ctx.tx_id)
        ctx = self.ctx()
        self.assertEqual(len(list(self.app.indexes.search_index(info.id, RangeSpec(2, 2), ctx))), 1)
        self.assertEqual(list(self.app.indexes.search_index(info.id, RangeSpec(9, 9), ctx)), [])
        self.app.transactions.commit(ctx.tx_id)
        delete = PlanNode("d", "Delete", expressions=[binary("<=", col("s.id"), lit(2))], properties=dict(
            table_id="students", input_columns=self.columns))
        self.assertEqual(self.run_plan(delete).affected_rows, 2)
        self.assertEqual([r[0] for r in self.run_plan(self.scan()).rows], [3, 4, 5])

    def test_failed_multirow_statement_rolls_back_previous_changes(self):
        self.insert([dict(id=1, name="old", score=0)])
        ctx = self.ctx()
        with self.assertRaises(DBError):
            self.insert([dict(id=2, name="new", score=1), dict(id=1, name="bad", score=2)], ctx)
        self.assertEqual(self.run_plan(self.scan()).rows, [[1, "old", 0]])

    def test_unique_index_failure_rolls_back_table(self):
        self.insert([dict(id=1, name="same", score=1)])
        ctx = self.ctx()
        self.app.executor.create_index(IndexSpec("idx_name", "students", ["name"], True), ctx)
        self.app.transactions.commit(ctx.tx_id)
        with self.assertRaises(DBError):
            self.insert([dict(id=2, name="same", score=2)])
        self.assertEqual(self.run_plan(self.scan()).rows, [[1, "same", 1]])

    def test_persistent_index_duplicate_range_boundaries(self):
        self.insert([dict(id=i, name=str(i), score=i//8) for i in range(48)])
        ctx = self.ctx()
        info = self.app.executor.create_index(IndexSpec("ix_score", "students", ["score"]), ctx)
        self.app.transactions.commit(ctx.tx_id)
        for low_inclusive in [True, False]:
            for high_inclusive in [True, False]:
                ctx = self.ctx()
                rids = list(self.app.executor.search_index(info.id, RangeSpec(2, 4, low_inclusive, high_inclusive), ctx))
                found = [self.app.records.fetch("students", rid, ctx).values["score"] for rid in rids]
                expected = [i//8 for i in range(48) if (i//8 >= 2 if low_inclusive else i//8 > 2)
                            and (i//8 <= 4 if high_inclusive else i//8 < 4)]
                self.assertEqual(found, expected)
                self.app.transactions.commit(ctx.tx_id)

    def test_check_unique_foreign_key_and_restrict(self):
        self.insert([dict(id=1, name="parent", score=1)])
        self.app.create_table(TableSchema("child", (Column("id", "INT"), Column("parent", "INT"), Column("v", "INT")), "id"), [
            Constraint("FOREIGN_KEY", ["parent"], "students", ["id"]),
            Constraint("UNIQUE", ["v"]),
            Constraint("CHECK", expression=binary(">=", col("v"), lit(0)))])
        def insert_child(row):
            return self.run_plan(PlanNode("i", "Insert", properties=dict(table_id="child", rows=[row])))
        insert_child(dict(id=1, parent=1, v=3))
        insert_child(dict(id=2, parent=None, v=None))
        insert_child(dict(id=3, parent=None, v=None))
        for row in [dict(id=4, parent=99, v=4), dict(id=4, parent=1, v=3), dict(id=4, parent=1, v=-1)]:
            with self.assertRaises(DBError):
                insert_child(row)
        with self.assertRaises(DBError) as raised:
            self.run_plan(PlanNode("d", "Delete", properties=dict(table_id="students", input_columns=self.columns)))
        self.assertEqual(raised.exception.code, "FOREIGN_KEY_VIOLATION")

    def test_index_scan_reduces_real_data_page_reads(self):
        self.insert([dict(id=i, name="n" * 180, score=i) for i in range(80)])
        ctx = self.ctx()
        info = self.app.executor.create_index(IndexSpec("idx_id", "students", ["id"]), ctx)
        self.app.transactions.commit(ctx.tx_id)
        baseline = PlanNode("f", "Filter", [self.scan()], self.columns, [binary("=", col("s.id"), lit(61))])
        indexed = PlanNode("i", "IndexScan", output_columns=self.columns, properties=dict(
            table_id="students", index_id=info.id, range=RangeSpec(61, 61)))
        self.app.pages.reset_cache()
        off = self.run_plan(baseline)
        self.app.pages.reset_cache()
        on = self.run_plan(indexed)
        self.assertEqual(off.rows, on.rows)
        self.assertGreater(off.metrics.data_page_reads, on.metrics.data_page_reads)
        self.assertGreater(on.metrics.index_page_reads, 0)


if __name__ == "__main__":
    unittest.main()
