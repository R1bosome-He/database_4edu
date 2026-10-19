"""C-only demo: bound plans are supplied directly, without a fake SQL parser."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .c_api import Expr, IndexSpec, PlanNode, RangeSpec, ResultColumn
from .integration.runtime import Runtime
from .models import Column, TableSchema


def demo(root):
    columns = [ResultColumn("s.id", "id", "INT", False), ResultColumn("s.name", "name", "TEXT"),
               ResultColumn("s.grade", "grade", "INT")]
    def col(slot):
        return Expr("ColumnRef", slot_id=slot)
    with Runtime(root, buffer_pages=2) as app:
        app.create_table(TableSchema("students", (Column("id", "INT", False), Column("name", "TEXT"),
                                                   Column("grade", "INT")), "id"))
        ctx = app.context()
        insert = PlanNode("insert", "Insert", properties=dict(table_id="students", rows=[
            dict(id=i, name=f"student_{i}_" + "x" * 160, grade=2021+i % 4) for i in range(80)]))
        result = app.executor.execute(insert, ctx)
        app.transactions.commit(ctx.tx_id)
        print(f"Inserted rows: {result.affected_rows}")
        ctx = app.context()
        index = app.executor.create_index(IndexSpec("idx_students_id", "students", ["id"], True), ctx)
        app.transactions.commit(ctx.tx_id)
        scan = PlanNode("scan", "SeqScan", output_columns=columns, properties=dict(table_id="students"))
        off = PlanNode("filter", "Filter", [scan], columns, [Expr("Binary", operator="=", args=[col("s.id"), Expr("Literal", 61)])])
        on = PlanNode("index", "IndexScan", output_columns=columns, properties=dict(
            table_id="students", index_id=index.id, range=RangeSpec(61, 61)))
        def run(plan):
            app.pages.reset_cache()
            ctx = app.context()
            result = app.executor.execute(plan, ctx)
            app.transactions.commit(ctx.tx_id)
            return result
        baseline, indexed = run(off), run(on)
        assert baseline.rows == indexed.rows
        print(json.dumps({"results_equal": True, "off": baseline.metrics.to_dict(),
                          "on": indexed.metrics.to_dict()}, indent=2))
        ctx = app.context()
        update = PlanNode("update", "Update", expressions=[Expr("Binary", operator="=", args=[col("s.id"), Expr("Literal", 61)])],
                          properties=dict(table_id="students", input_columns=columns,
                                          assignments={"name": Expr("Literal", "temporary")}))
        app.executor.execute(update, ctx)
        app.transactions.rollback(ctx.tx_id)
        assert run(on).rows == baseline.rows
        print("Rollback restored table and index: OK")
        app.transactions.checkpoint()
    with Runtime(root) as reopened:
        print("Reopen/recovery:", reopened.recovery_summary)


def main():
    parser = argparse.ArgumentParser(description="C module bound-plan/index/transaction demo")
    parser.add_argument("--db", type=Path, help="New empty directory; existing data is never reset")
    args = parser.parse_args()
    if args.db:
        if args.db.exists() and any(args.db.iterdir()):
            parser.error("--db must name a new or empty directory")
        demo(args.db)
    else:
        with tempfile.TemporaryDirectory(prefix="edudb_c_demo_") as path:
            demo(path)


if __name__ == "__main__":
    main()
