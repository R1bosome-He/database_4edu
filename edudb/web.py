"""Local authenticated D workbench. python -m edudb.web --demo"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
from dataclasses import asdict, is_dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .c_api import DBError
from .workbench.service import Workbench
from .workbench.report import render_report

STATIC = Path(__file__).with_name("workbench") / "static"


def json_default(value):
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(type(value).__name__)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "EduDB"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass

    def respond(self, status, payload, content_type="application/json; charset=utf-8", headers=None):
        raw = json.dumps(payload, ensure_ascii=False, default=json_default).encode() if isinstance(payload, (dict, list)) else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        try:
            self.end_headers()
            self.wfile.write(raw)
        except ConnectionError:
            # The browser may close a tab after a completed transaction.
            self.close_connection = True

    def token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        return cookie["edudb_session"].value if "edudb_session" in cookie else ""

    def _handle(self, post=False):
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed:
            raise DBError("FORBIDDEN", "Invalid Host")
        path = urlsplit(self.path).path
        if not post and not path.startswith("/api/"):
            files = {"/": "index.html", "/app.js": "app.js", "/styles.css": "styles.css", "/lucide.min.js": "lucide.min.js", "/brand.svg": "brand.svg"}
            if path not in files:
                return self.respond(404, {"error": {"code": "NOT_FOUND", "message": "Not found"}})
            target = STATIC / files[path]
            return self.respond(200, target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        data = {}
        if post:
            if self.headers.get("Origin") not in {"http://" + host for host in allowed}:
                raise DBError("FORBIDDEN", "Same-origin requests required")
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise DBError("INVALID_REQUEST", "JSON required")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise DBError("INVALID_REQUEST", "Request exceeds limit")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise DBError("INVALID_REQUEST", "JSON object required")
        app, token = self.server.app, self.token()
        with app.lock:
            if path == "/api/login" and post:
                # Replacement login invalidates the previous session and its transaction.
                new_token = app.login(data.get("username"), data.get("password"), self.client_address[0])
                if token in app.sessions:
                    app._drop(token)
                return self.respond(200, app.state(new_token), headers={"Set-Cookie": f"edudb_session={new_token}; HttpOnly; SameSite=Strict; Path=/"})
            session = app.session(token)
            if post and self.headers.get("X-CSRF-Token") != session.csrf:
                raise DBError("FORBIDDEN", "Invalid CSRF token")
            if path == "/api/state" and not post:
                return self.respond(200, app.state(token))
            if path == "/api/query" and post:
                return self.respond(200, app.execute(token, data.get("sql"), data.get("optimizer", True) is True))
            if path == "/api/compare" and post:
                return self.respond(200, app.compare(token, data.get("sql"), data.get("repeats", 3), data.get("cache", "cold")))
            if path == "/api/logout" and post:
                app.logout(token)
                return self.respond(200, {"ok": True}, headers={"Set-Cookie": "edudb_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/"})
            if path == "/api/users":
                if post:
                    app.create_user(token, data.get("username"), data.get("password"), data.get("role", "member"))
                return self.respond(200, app.users(token))
            if path == "/api/grants" and post:
                app.grant(token, data.get("username"), data.get("table_id"), data.get("actions"))
                return self.respond(200, {"ok": True})
            if path == "/api/audit" and not post:
                return self.respond(200, app.events(token))
            if path == "/api/report.html" and not post:
                return self.respond(200, render_report(app.report(token)), "text/html; charset=utf-8",
                                    headers={"Content-Disposition": 'attachment; filename="edudb-report.html"'})
            if path == "/api/report" and not post:
                return self.respond(200, app.report(token), headers={"Content-Disposition": 'attachment; filename="edudb-report.json"'})
            self.respond(404, {"error": {"code": "NOT_FOUND", "message": "Not found"}})

    def dispatch(self, post=False):
        try:
            self._handle(post)
        except DBError as exc:
            try:
                app = self.server.app
                with app.lock:
                    session = app.sessions.get(self.token())
                    app.audit.append("HTTP_REJECTED", session.username if session else None, False,
                                     error_code=exc.code)
            except Exception:
                pass
            status = 401 if exc.code in {"UNAUTHENTICATED", "INVALID_CREDENTIALS"} else 403 if exc.code == "FORBIDDEN" else 429 if exc.code == "RATE_LIMITED" else 400
            self.respond(status, {"error": exc.to_dict()})
        except (ValueError, TypeError, KeyError):
            self.respond(400, {"error": {"code": "INVALID_REQUEST", "message": "Invalid request"}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.respond(500, {"error": {"code": "SERVER_ERROR", "message": "Request failed; changes may have executed. Check state before retrying."}})

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch(True)


def main():
    parser = argparse.ArgumentParser(description="EduDB authenticated local workbench")
    parser.add_argument("--db", type=Path, default=Path(".edudb_workbench"))
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--demo", action="store_true", help="Seed a new empty database; never reset existing data")
    parser.add_argument("--test-evidence", type=Path, help="Source-bound evidence JSON generated by edudb.workbench.evidence")
    args = parser.parse_args()
    app = Workbench(args.db, os.environ.get("EDUDB_ADMIN_PASSWORD"), args.demo, args.test_evidence)
    try:
        server = Server(("127.0.0.1", args.port), app)
        print(f"EduDB Workbench: http://127.0.0.1:{server.server_address[1]}/", flush=True)
        if app.bootstrap_password:
            print(f"First-run account: admin / {app.bootstrap_password}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        app.close()


if __name__ == "__main__":
    main()
