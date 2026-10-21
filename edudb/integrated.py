"""Local A+B+C SQL shell. No authentication; not a network service."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .c_api import DBError
from .integration.contract_runtime import ContractRuntime
from .sql import SqlSession


def output(session, sql):
    result = session.execute(sql)
    print(json.dumps(dict(columns=[c.name for c in result.columns], rows=result.rows,
                          affected_rows=result.affected_rows, message=result.message,
                          metrics=result.metrics.to_dict()), ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="A+B+C local SQL shell (no Web/auth)")
    parser.add_argument("--db", type=Path, default=Path(".edudb_integrated"))
    parser.add_argument("--sql", action="append", help="One statement per option; may be repeated")
    parser.add_argument("--optimizer", choices=("on", "off"), default="on")
    args = parser.parse_args(argv)
    try:
        with ContractRuntime(args.db) as runtime:
            session = SqlSession(runtime, optimizer_enabled=args.optimizer == "on")
            if args.sql:
                for sql in args.sql:
                    output(session, sql)
                return 0
            print("A+B+C SQL shell. End a statement with ';'. Type .quit to exit.")
            print("Data directory:", args.db.resolve())
            statement = []
            while True:
                try:
                    line = input("sql> " if not statement else " ... ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not statement and line.strip() == ".quit":
                    break
                if not line.strip():
                    continue
                statement.append(line)
                if line.rstrip().endswith(";"):
                    try:
                        output(session, "\n".join(statement))
                    except DBError as exc:
                        print(json.dumps(exc.to_dict(), ensure_ascii=False))
                        if runtime.transactions.needs_recovery:
                            print("Recovery required: close and reopen the database.")
                            return 1
                    finally:
                        statement.clear()
            return 0
    except (DBError, OSError) as exc:
        parser.exit(1, f"{getattr(exc, 'code', 'IO_ERROR')}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
