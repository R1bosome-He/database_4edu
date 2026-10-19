from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .database import Database
from .models import QueryResult


STATIC_DIR = Path(__file__).with_name("web_static")
DEFAULT_SQL = "COMPARE SELECT id, name, major FROM students WHERE id = 480;"


class WebAppState:
    def __init__(self, db_path: Path, buffer_pages: int = 16) -> None:
        self.db_path = db_path
        self.buffer_pages = buffer_pages
        self.db = Database(db_path, buffer_pages=buffer_pages)
        self.sessions: dict[str, str] = {}
        self.lock = threading.RLock()

    def reset_demo(self) -> dict[str, Any]:
        with self.lock:
            self.db = Database.reset(self.db_path, buffer_pages=self.buffer_pages)
            seed_demo_data(self.db)
            self.sessions.clear()
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "optimizer": self.db.optimizer_enabled,
            "transaction_active": self.db._transaction_backup is not None,
            "tables": [
                {
                    "name": schema.name,
                    "columns": [column.to_dict() for column in schema.columns],
                    "primary_key": schema.primary_key,
                    "row_count": schema.row_count,
                    "page_count": schema.page_count,
                    "indexes": [index.to_dict() for index in schema.indexes],
                }
                for schema in self.db.catalog.tables.values()
            ],
            "audit": read_audit_tail(self.db.root / "audit.log", limit=8),
        }


class EduDBMSHandler(BaseHTTPRequestHandler):
    server_version = "EduDBMSWeb/0.1"

    @property
    def app(self) -> WebAppState:
        return self.server.app  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self._json({"ok": True, "user": self._current_user(), **self.app.snapshot()})
            return
        if path == "/api/audit":
            self._json({"ok": True, "audit": read_audit_tail(self.app.db.root / "audit.log", limit=30)})
            return
        if path == "/api/export-report":
            self._download_report()
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/login":
                self._login()
                return
            if path == "/api/logout":
                self._logout()
                return
            if path == "/api/reset-demo":
                snapshot = self.app.reset_demo()
                self._json({"ok": True, "message": "Demo database loaded", **snapshot})
                return
            if path == "/api/execute":
                self._execute()
                return
            self._json({"ok": False, "error": "Not found"}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    def log_message(self, fmt: str, *args: object) -> None:
        if sys.stderr:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _login(self) -> None:
        payload = self._read_json()
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        with self.app.lock:
            if not self.app.db.login(username, password):
                self._json({"ok": False, "error": "Login failed"}, status=HTTPStatus.UNAUTHORIZED)
                return
            token = secrets.token_urlsafe(24)
            self.app.sessions[token] = username.lower()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", f"edudb_session={token}; Path=/; SameSite=Lax")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "user": username.lower()}, ensure_ascii=False).encode("utf-8"))

    def _logout(self) -> None:
        token = self._session_token()
        if token:
            self.app.sessions.pop(token, None)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", "edudb_session=; Path=/; Max-Age=0; SameSite=Lax")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"))

    def _execute(self) -> None:
        payload = self._read_json()
        sql = str(payload.get("sql", "")).strip()
        if not sql:
            self._json({"ok": False, "error": "SQL is empty"}, status=HTTPStatus.BAD_REQUEST)
            return
        user = self._current_user()
        with self.app.lock:
            result = self.app.db.execute(sql, user=user)
            snapshot = self.app.snapshot()
        self._json(
            {
                "ok": True,
                "user": user,
                "result": serialize_result(result),
                **snapshot,
            }
        )

    def _serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            relative = "index.html"
        else:
            relative = unquote(path).lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.exists():
            self._json({"ok": False, "error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        content_type = _content_type(target)
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _download_report(self) -> None:
        report = build_test_report(self.app.snapshot(), self.app.db.root)
        encoded = report.encode("utf-8")
        filename = f"edudbms_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        data = self.rfile.read(length).decode("utf-8")
        return json.loads(data)

    def _json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _current_user(self) -> str:
        token = self._session_token()
        if token and token in self.app.sessions:
            return self.app.sessions[token]
        return "admin"

    def _session_token(self) -> str | None:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "edudb_session":
                return value
        return None


def seed_demo_data(db: Database) -> None:
    db.execute("CREATE TABLE students (id INT, name TEXT, major TEXT, grade INT, PRIMARY KEY (id));")
    db.execute("CREATE TABLE enrollments (student_id INT, course TEXT, score FLOAT);")
    majors = ["database", "security", "ai", "network"]
    courses = ["DBMS", "Security", "Systems", "Compiler", "AI", "Network"]
    student_count = 640
    enrollment_count = 1280
    for i in range(1, student_count + 1):
        db.execute(
            "INSERT INTO students (id, name, major, grade) "
            f"VALUES ({i}, 'student_{i}', '{majors[i % len(majors)]}', {2021 + i % 4});"
        )
    for i in range(1, enrollment_count + 1):
        db.execute(
            "INSERT INTO enrollments (student_id, course, score) "
            f"VALUES ({(i % student_count) + 1}, '{courses[i % len(courses)]}', {60 + (i % 41)});"
        )
    db.execute("CREATE INDEX idx_students_id ON students (id);")
    db.execute("CREATE INDEX idx_enrollments_student_id ON enrollments (student_id);")
    try:
        db.execute("CREATE USER reader PASSWORD 'reader123';")
        db.execute("GRANT SELECT ON students TO reader;")
    except ValueError:
        pass


def serialize_result(result: QueryResult) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": result.kind,
        "message": result.message,
        "columns": result.columns,
        "rows": result.rows[:200],
        "row_count": len(result.rows),
        "plan": result.plan,
        "plan_tree": plan_to_tree(result.plan),
        "metrics": result.metrics,
    }
    if result.comparisons:
        data["comparisons"] = [serialize_result(item) for item in result.comparisons]
    return data


def plan_to_tree(plan: str) -> list[dict[str, Any]]:
    roots: list[dict[str, Any]] = []
    stack: list[tuple[int, dict[str, Any]]] = []
    for line in plan.splitlines():
        if not line.strip():
            continue
        level = (len(line) - len(line.lstrip(" "))) // 2
        text = line.strip()
        label, _, raw_detail = text.partition(" [")
        node = {
            "label": label,
            "detail": raw_detail[:-1] if raw_detail.endswith("]") else raw_detail,
            "children": [],
        }
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1]["children"].append(node)
        else:
            roots.append(node)
        stack.append((level, node))
    return roots


def build_test_report(snapshot: dict[str, Any], root: Path) -> str:
    audit = read_audit_tail(root / "audit.log", limit=16)
    table_lines = [
        f"| {table['name']} | {table['row_count']} | {table['page_count']} | "
        f"{', '.join(index['name'] + '(' + index['column'] + ')' for index in table['indexes']) or '无'} |"
        for table in snapshot.get("tables", [])
    ]
    audit_lines = [
        f"- {entry.get('time', '')}: `{entry.get('user', '')}` "
        f"{'成功' if entry.get('success') else '失败'}执行 `{entry.get('sql', '')}`，耗时 {entry.get('elapsed_ms', '-')} ms"
        for entry in audit
    ]
    return "\n".join(
        [
            "# EduDBMS Web 演示测试报告",
            "",
            f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "## 当前数据目录",
            "",
            "| 表名 | 行数 | 页数 | 索引 |",
            "| --- | ---: | ---: | --- |",
            *(table_lines or ["| 无 | 0 | 0 | 无 |"]),
            "",
            "## 已验证功能",
            "",
            "- 页式存储和 LRU 缓冲池。",
            "- B+ 树索引和索引查询。",
            "- `EXPLAIN` 与 `COMPARE` 执行计划对比。",
            "- `ORDER BY` 排序查询。",
            "- `UPDATE`、`DELETE` 数据修改。",
            "- `BEGIN`、`COMMIT`、`ROLLBACK` 事务雏形。",
            "- 用户登录、表级权限和审计日志。",
            "",
            "## 建议演示 SQL",
            "",
            "```sql",
            "COMPARE SELECT id, name, major FROM students WHERE id = 480;",
            "COMPARE SELECT students.name, enrollments.course, enrollments.score FROM students JOIN enrollments ON students.id = enrollments.student_id WHERE students.id = 480;",
            "SELECT id, name, grade FROM students WHERE major = 'database' ORDER BY grade DESC, id ASC;",
            "BEGIN;",
            "UPDATE students SET major = 'security' WHERE id = 12;",
            "ROLLBACK;",
            "DELETE FROM enrollments WHERE score < 62;",
            "```",
            "",
            "## 最近审计记录",
            "",
            *(audit_lines or ["- 暂无审计记录。"]),
            "",
        ]
    )


def read_audit_tail(path: Path, limit: int = 12) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    entries: list[dict[str, Any]] = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".html":
        return "text/html; charset=utf-8"
    if suffix == ".css":
        return "text/css; charset=utf-8"
    if suffix == ".js":
        return "text/javascript; charset=utf-8"
    if suffix == ".svg":
        return "image/svg+xml"
    return "application/octet-stream"


class EduDBMSServer(ThreadingHTTPServer):
    app: WebAppState


def run(host: str, port: int, db_path: Path, reset_demo: bool = False) -> None:
    app = WebAppState(db_path)
    if reset_demo or not app.db.catalog.tables:
        app.reset_demo()
    server = EduDBMSServer((host, port), EduDBMSHandler)
    server.app = app
    _safe_print(f"EduDBMS Web Console running at http://{host}:{port}")
    _safe_print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _safe_print("\nServer stopped.")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    _prefer_utf8_stdio()
    parser = argparse.ArgumentParser(description="EduDBMS Web Console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=".edudb_web")
    parser.add_argument("--reset-demo", action="store_true", help="rebuild demo data on startup")
    args = parser.parse_args(argv)
    run(args.host, args.port, Path(args.db), reset_demo=args.reset_demo)
    return 0


def _prefer_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _safe_print(message: str) -> None:
    if sys.stdout:
        print(message, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
