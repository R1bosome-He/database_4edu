from __future__ import annotations

import argparse
import sys

from .database import Database
from .demo import run_demo
from .formatting import format_result


HELP = """Commands:
  .help                         show this help
  .login USER PASSWORD           switch current user
  .optimizer on|off              toggle query optimizer
  .compare SELECT ...            compare optimizer OFF and ON
  .exit                          quit

SQL subset:
  CREATE TABLE t (id INT, name TEXT, PRIMARY KEY (id));
  INSERT INTO t (id, name) VALUES (1, 'Ada');
  CREATE INDEX idx_t_id ON t (id);
  SELECT * FROM t WHERE id = 1;
  SELECT * FROM t ORDER BY id DESC;
  UPDATE t SET name = 'Ada' WHERE id = 1;
  DELETE FROM t WHERE id = 1;
  BEGIN; COMMIT; ROLLBACK;
  EXPLAIN OPTIMIZER ON SELECT * FROM t WHERE id = 1;
  CREATE USER alice PASSWORD 'pw';
  GRANT SELECT|INSERT|UPDATE|DELETE|ALL ON t TO alice;
"""


def main(argv: list[str] | None = None) -> int:
    _prefer_utf8_stdio()
    parser = argparse.ArgumentParser(description="Teaching DBMS CLI")
    parser.add_argument("--db", default=".edudb", help="database directory")
    parser.add_argument("--execute", "-e", help="execute one SQL statement")
    parser.add_argument("--user", default="admin", help="username")
    parser.add_argument("--password", default="admin", help="password")
    parser.add_argument("--demo", action="store_true", help="run built-in benchmark demo")
    args = parser.parse_args(argv)

    if args.demo:
        print(run_demo())
        return 0

    db = Database(args.db)
    if not db.login(args.user, args.password):
        print("Login failed", file=sys.stderr)
        return 2

    if args.execute:
        try:
            print(format_result(db.execute(args.execute, user=args.user)))
            return 0
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    return repl(db, args.user)


def repl(db: Database, user: str) -> int:
    print("EduDBMS ready. Type .help for commands.")
    buffer: list[str] = []
    while True:
        prompt = f"{user}> " if not buffer else "... "
        try:
            line = input(prompt)
        except EOFError:
            print()
            return 0
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(".") and not buffer:
            should_exit, user = _handle_meta(db, user, stripped)
            if should_exit:
                return 0
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            sql = "\n".join(buffer)
            buffer = []
            try:
                print(format_result(db.execute(sql, user=user)))
            except Exception as exc:
                print(f"Error: {exc}")


def _handle_meta(db: Database, user: str, command: str) -> tuple[bool, str]:
    parts = command.split()
    name = parts[0].lower()
    if name in {".exit", ".quit"}:
        return True, user
    if name == ".help":
        print(HELP)
        return False, user
    if name == ".login" and len(parts) == 3:
        if db.login(parts[1], parts[2]):
            print(f"Logged in as {parts[1].lower()}")
            return False, parts[1].lower()
        print("Login failed")
        return False, user
    if name == ".optimizer" and len(parts) == 2 and parts[1].lower() in {"on", "off"}:
        sql = f"SET OPTIMIZER {parts[1].upper()};"
        print(format_result(db.execute(sql, user=user)))
        return False, user
    if name == ".compare" and len(command) > len(".compare "):
        sql = "COMPARE " + command[len(".compare ") :]
        print(format_result(db.execute(sql, user=user)))
        return False, user
    print("Unknown command. Type .help for commands.")
    return False, user


def _prefer_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
