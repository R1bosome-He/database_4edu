"""Module B demo: everything is driven from SQL text, not hand-built plans.

Shows the full pipeline tokenize -> parse -> bind -> plan -> optimize running on
module C's real executor, and the optimizer-on/off experiment (equal results,
fewer real data-page reads with an index). Run from the repo root:

    python -m edudb.sql_demo
    python -m edudb.sql_demo --db .sql_demo_data   # keep data in a new empty dir
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from .integration.runtime import Runtime
from .sql import SqlSession, compile_sql
from .sql.catalog import RuntimeCatalog
from .sql.compiler import CompileOptions, render_plan


def _print_rows(title, result):
    header = ", ".join(c.name for c in result.columns)
    print(f"\n{title}")
    print("  columns: " + header)
    for row in result.rows:
        print("  " + " | ".join("NULL" if v is None else str(v) for v in row))


def demo(root):
    with Runtime(root, buffer_pages=4) as app:
        session = SqlSession(app)
        session.execute("CREATE TABLE students (id INT PRIMARY KEY, name TEXT NOT NULL, score INT)")
        session.execute("CREATE TABLE enroll (sid INT, course TEXT)")
        padding = "x" * 160
        session.execute("INSERT INTO students VALUES " + ",".join(
            f"({i}, 'student_{i}_{padding}', {60 + i % 40})" for i in range(80)))
        session.execute("INSERT INTO enroll VALUES (1,'db'),(1,'os'),(3,'db'),(7,'ai'),(9,'db')")

        _print_rows("SELECT with WHERE + ORDER BY + LIMIT",
                    session.execute("SELECT id, score FROM students WHERE score >= 95 "
                                    "ORDER BY score DESC, id ASC LIMIT 5"))
        _print_rows("Equi-join (optimizer turns it into a hash join)",
                    session.execute("SELECT s.id, e.course FROM students s JOIN enroll e "
                                    "ON s.id = e.sid ORDER BY s.id, e.course"))
        _print_rows("Aggregate with GROUP BY / HAVING",
                    session.execute("SELECT score, COUNT(*) AS n FROM students "
                                    "GROUP BY score HAVING COUNT(*) > 1 ORDER BY score LIMIT 5"))

        print("\nOptimizer experiment: SELECT name FROM students WHERE id = 61")
        session.execute("CREATE INDEX idx_students_id ON students (id)")
        catalog = RuntimeCatalog(app)
        query = "SELECT name FROM students WHERE id = 61"
        for label, enabled in [("OFF", False), ("ON", True)]:
            compiled = compile_sql(query, catalog, CompileOptions(enabled))
            print(f"\n  plan (optimizer {label}):")
            print("    " + render_plan(compiled.plan).replace("\n", "\n    "))
        app.pages.reset_cache()
        off = session.execute(query, optimizer_enabled=False)
        app.pages.reset_cache()
        on = session.execute(query, optimizer_enabled=True)
        print("\n  results equal:", off.rows == on.rows, "->", on.rows)
        print(f"  data_page_reads  off={off.metrics.data_page_reads}  on={on.metrics.data_page_reads}")
        print(f"  index_page_reads off={off.metrics.index_page_reads}  on={on.metrics.index_page_reads}")

        print("\nTransaction rollback")
        session.execute("BEGIN")
        session.execute("UPDATE students SET score = -1 WHERE id = 61")
        print("  inside tx:", session.execute("SELECT score FROM students WHERE id = 61").rows)
        session.execute("ROLLBACK")
        print("  after rollback:", session.execute("SELECT score FROM students WHERE id = 61").rows)


def main():
    parser = argparse.ArgumentParser(description="Module B SQL compiler/optimizer demo")
    parser.add_argument("--db", type=Path, help="New empty directory; existing data is never reset")
    args = parser.parse_args()
    if args.db:
        if args.db.exists() and any(args.db.iterdir()):
            parser.error("--db must name a new or empty directory")
        demo(args.db)
    else:
        with tempfile.TemporaryDirectory(prefix="edudb_sql_demo_") as path:
            demo(path)


if __name__ == "__main__":
    main()
