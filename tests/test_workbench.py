from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from edudb.c_api import DBError
from edudb.web import Server
from edudb.workbench.audit import AuditLog
from edudb.workbench.service import Workbench
from edudb.workbench.evidence import load_evidence, source_hash
from edudb.workbench.report import render_report


class WorkbenchTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = Workbench(self.root, "A-test-password-123!")
        self.addCleanup(self.app.close)
        self.token = self.app.login("admin", "A-test-password-123!")
        self.app.execute(self.token, "CREATE TABLE students (id INT PRIMARY KEY, name TEXT)")
        self.app.execute(self.token, "INSERT INTO students VALUES (1,'private-value'),(2,'other')")

    def code(self, code, function, *args, **kwargs):
        with self.assertRaises(DBError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def member(self):
        self.app.create_user(self.token, "analyst", "Analyst-password-123", "member")
        return self.app.login("analyst", "Analyst-password-123")

    def test_identity_password_hash_and_no_default_auth(self):
        raw = (self.root / "security/users.json").read_text()
        self.assertNotIn("A-test-password", raw)
        self.code("UNAUTHENTICATED", self.app.state, "")
        self.code("INVALID_CREDENTIALS", self.app.login, "admin", "wrong")
        self.assertEqual(self.app.state(self.token)["user"]["role"], "admin")

    def test_rate_limit_login(self):
        for _ in range(5):
            self.code("INVALID_CREDENTIALS", self.app.login, "admin", "wrong", "test-peer")
        self.code("RATE_LIMITED", self.app.login, "admin", "A-test-password-123!", "test-peer")

    def test_table_level_grant_and_revoke(self):
        member = self.member()
        self.assertEqual(self.app.state(member)["tables"], [])
        self.code("FORBIDDEN", self.app.execute, member, "SELECT * FROM students")
        table = self.app.runtime.storage.get_table("students")
        self.app.grant(self.token, "analyst", table.id, ["SELECT"])
        self.assertEqual(len(self.app.execute(member, "SELECT * FROM students")["rows"]), 2)
        self.code("FORBIDDEN", self.app.execute, member, "DELETE FROM students")
        self.app.grant(self.token, "analyst", table.id, [])
        self.code("FORBIDDEN", self.app.execute, member, "SELECT * FROM students")

    def test_authorization_applies_to_explain_join_and_subquery(self):
        member = self.member()
        self.app.execute(self.token, "CREATE TABLE restricted (id INT)")
        table = self.app.runtime.storage.get_table("students")
        self.app.grant(self.token, "analyst", table.id, ["SELECT"])
        for sql in ("EXPLAIN SELECT * FROM restricted", "SELECT s.id FROM students s JOIN restricted r ON s.id=r.id",
                    "SELECT * FROM students WHERE id IN (SELECT id FROM restricted)"):
            self.code("FORBIDDEN", self.app.execute, member, sql)

    def test_unauthorized_admin_endpoints(self):
        member = self.member()
        self.code("FORBIDDEN", self.app.users, member)
        self.code("FORBIDDEN", self.app.events, member)
        self.code("FORBIDDEN", self.app.create_user, member, "other", "Other-pass-12345", "admin")
        self.code("FORBIDDEN", self.app.execute, member, "CREATE TABLE other (id INT)")

    def test_logout_and_expiry_rollback(self):
        self.app.execute(self.token, "BEGIN")
        self.app.execute(self.token, "UPDATE students SET name='temporary' WHERE id=1")
        self.app.logout(self.token)
        self.code("UNAUTHENTICATED", self.app.state, self.token)
        self.token = self.app.login("admin", "A-test-password-123!")
        self.assertEqual(self.app.execute(self.token, "SELECT name FROM students WHERE id=1")["rows"], [["private-value"]])
        self.app.execute(self.token, "BEGIN")
        self.app.execute(self.token, "DELETE FROM students")
        self.app.sessions[self.token].touched -= 1801
        self.code("UNAUTHENTICATED", self.app.state, self.token)
        self.assertFalse(self.app.runtime.transactions.has_active_transactions())

    def test_session_cannot_commit_another_session(self):
        other = self.app.login("admin", "A-test-password-123!")
        self.app.execute(self.token, "BEGIN")
        self.code("NO_TRANSACTION", self.app.execute, other, "COMMIT")
        self.assertTrue(self.app.state(self.token)["transaction_active"])
        self.app.execute(self.token, "ROLLBACK")

    def test_compare_real_metrics_and_no_side_effects(self):
        self.app.execute(self.token, "CREATE INDEX ix ON students(id)")
        result = self.app.compare(self.token, "SELECT name FROM students WHERE id=1", 3, "cold")
        self.assertTrue(result["equivalent"])
        self.assertEqual([sample["mode"] for sample in result["samples"]], ["off","on","on","off","off","on"])
        self.assertGreaterEqual(result["summary"]["on"]["planning_ms"], 0)
        self.assertEqual(len(result["samples"]), 6)
        self.code("READ_ONLY_EXPERIMENT", self.app.compare, self.token, "DELETE FROM students")
        self.app.execute(self.token, "BEGIN")
        self.code("EXPERIMENT_BUSY", self.app.compare, self.token, "SELECT * FROM students")
        self.app.execute(self.token, "ROLLBACK")

    def test_report_audit_redaction_and_chain(self):
        self.app.compare(self.token, "SELECT id FROM students WHERE name='private-value'", 1)
        report = self.app.report(self.token)
        raw = json.dumps(report)
        self.assertNotIn("private-value", raw)
        self.assertNotIn("A-test-password", raw)
        self.assertEqual(report["tests"]["status"], "not_run")
        audit = (self.root / "security/audit.jsonl").read_text()
        self.assertNotIn("private-value", audit)
        self.assertNotIn("A-test-password", audit)
        self.assertEqual(AuditLog(self.root / "security/audit.jsonl").tail, self.app.audit.tail)
        path = self.root / "tampered.jsonl"
        path.write_text(audit.replace("SQL_REQUEST", "SQL_CHANGED", 1))
        self.code("AUDIT_CORRUPTION", AuditLog, path)

    def test_failed_sql_is_audited_and_does_not_return_old_rows(self):
        self.code("SYNTAX_ERROR", self.app.execute, self.token, "broken SQL")
        last = self.app.audit.events[-1]
        self.assertFalse(last["success"])
        self.assertEqual(last["error_code"], "SYNTAX_ERROR")

    def test_report_html_escapes_and_marks_stale_evidence(self):
        report = self.app.report(self.token)
        report["user"] = "<script>alert(1)</script>"
        raw = render_report(report).decode("utf-8")
        self.assertNotIn("<script>", raw)
        self.assertIn("&lt;script&gt;", raw)
        self.assertIn("not_run", raw)
        path = self.root / "evidence.json"
        path.write_text(json.dumps({"status":"passed", "source_sha256":"outdated", "tests_run":999}))
        self.assertEqual(load_evidence(path)["status"], "stale")
        path.write_text(json.dumps({"status":"passed", "source_sha256":source_hash(), "tests_run":1}))
        self.app.evidence_path = path
        self.assertEqual(self.app.report(self.token)["tests"]["tests_run"], 1)

    def test_grant_changes_blocked_during_active_transaction(self):
        self.member()
        self.app.execute(self.token, "BEGIN")
        table = self.app.runtime.storage.get_table("students")
        self.code("ACTIVE_TRANSACTIONS", self.app.grant, self.token, "analyst", table.id, ["SELECT"])
        self.app.execute(self.token, "ROLLBACK")

    def test_warm_compare_and_ordered_duplicate_rows(self):
        report = self.app.compare(self.token, "SELECT name FROM students ORDER BY name", 2, "warm")
        self.assertTrue(report["equivalent"])
        self.assertEqual(report["comparison"], "sequence")
        self.assertEqual(report["summary"]["on"]["data_page_reads"], 0)
        self.assertEqual(Workbench._bag([[1],[1],[2]]), Workbench._bag([[2],[1],[1]]))
        self.assertNotEqual(Workbench._bag([[1],[1]]), Workbench._bag([[1]]))

    def test_reopen_retains_accounts_grants_and_audit(self):
        member = self.member()
        table = self.app.runtime.storage.get_table("students")
        self.app.grant(self.token, "analyst", table.id, ["SELECT"])
        self.app.logout(member)
        self.app.close()
        reopened = Workbench(self.root)
        try:
            token = reopened.login("analyst", "Analyst-password-123")
            self.assertEqual(len(reopened.execute(token, "SELECT * FROM students")["rows"]), 2)
            self.assertTrue(any(e["action"] == "GRANT" for e in reopened.audit.events))
            self.assertIsNone(reopened.bootstrap_password)
        finally:
            reopened.close()

    def test_http_auth_origin_csrf_headers_and_static_allowlist(self):
        server = Server(("127.0.0.1", 0), self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        connection = http.client.HTTPConnection("127.0.0.1", port)
        self.addCleanup(connection.close)
        def request(path, data=None, csrf=None, origin=None, cookie=None):
            headers = {}
            if data is not None:
                headers["Content-Type"] = "application/json"
                if origin:
                    headers["Origin"] = origin
            if csrf:
                headers["X-CSRF-Token"] = csrf
            if cookie:
                headers["Cookie"] = cookie
            connection.request("POST" if data is not None else "GET", path, json.dumps(data) if data is not None else None, headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        self.assertEqual(request("/api/state")[0], 401)
        self.assertEqual(request("/../../security/users.json")[0], 404)
        payload = dict(username="admin", password="A-test-password-123!")
        self.assertEqual(request("/api/login", payload, origin="http://evil.test")[0], 403)
        status, headers, raw = request("/api/login", payload, origin=f"http://127.0.0.1:{port}")
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        cookie = headers["Set-Cookie"].split(";")[0]
        data = json.loads(raw)
        self.assertEqual(request("/api/query", {"sql":"SELECT * FROM students"}, origin=f"http://127.0.0.1:{port}", cookie=cookie)[0], 403)
        self.assertEqual(request("/api/query", {"sql":"SELECT * FROM students"}, csrf=data["csrf"], origin=f"http://127.0.0.1:{port}", cookie=cookie)[0], 200)


if __name__ == "__main__":
    unittest.main()
