from __future__ import annotations

import base64
import hashlib
import http.client
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from edudb.c_api import DBError,Expr
from edudb.integration.runtime import Runtime
from edudb.integration.contract_runtime import ContractRuntime
from edudb.sql import SqlSession
from edudb.workbench.service import Workbench
from edudb.workbench.security import IdentityStore
from edudb.protection import FileProtector
from edudb.migrate_workbench import migrate
from edudb.web import Server


class RequestedSqlFixes(unittest.TestCase):
    def each_runtime(self,check):
        for cls in (Runtime,ContractRuntime):
            with self.subTest(runtime=cls.__name__),tempfile.TemporaryDirectory() as folder:
                with cls(folder) as runtime:
                    check(runtime,SqlSession(runtime))

    def test_range_intersection_and_strict_bounds(self):
        def check(runtime,s):
            s.execute("CREATE TABLE t(id INT PRIMARY KEY)")
            s.execute("INSERT INTO t VALUES "+",".join(f"({i})" for i in range(1,9)))
            s.execute("CREATE INDEX ix ON t(id)")
            reference=sqlite3.connect(":memory:")
            try:
                reference.execute("CREATE TABLE t(id INT)")
                reference.executemany("INSERT INTO t VALUES (?)",[(i,) for i in range(1,9)])
                for condition in ["id>5 AND id>1","id>1 AND id>5","id<3 AND id<6","id>5 AND id>=5",
                                  "id>=5 AND id>5","id<5 AND id<=5","id<=5 AND id<5",
                                  "id>5 AND id<3 AND id>1","id>=5 AND id>=1","id=5 AND id>5"]:
                    query="SELECT id FROM t WHERE "+condition+" ORDER BY id"
                    expected=[list(row) for row in reference.execute(query)]
                    self.assertEqual(s.execute(query,False).rows,expected)
                    self.assertEqual(s.execute(query,True).rows,expected)
            finally: reference.close()
        self.each_runtime(check)

    def test_signed_integer_boundaries_and_remainder(self):
        def check(runtime,s):
            self.assertEqual(s.execute("SELECT -9223372036854775808").rows,[[-2**63]])
            self.assertEqual(s.execute("SELECT 9223372036854775807").rows,[[2**63-1]])
            for expression,expected in [("-5%3",-2),("5%-3",2),("-5%-3",-2),("-9223372036854775808%3",-2)]:
                self.assertEqual(s.execute("SELECT "+expression).rows,[[expected]])
            for expression in ["9223372036854775808","-9223372036854775809","9223372036854775807+1"]:
                with self.assertRaises(DBError): s.execute("SELECT "+expression)
            ctx=runtime.context()
            self.assertEqual(runtime.executor.evaluate(Expr("Unary",operator="-",args=[Expr("Literal",2**63)]),{},ctx),-2**63)
            runtime.transactions.rollback(ctx.tx_id)
        self.each_runtime(check)

    def test_views_nested_join_and_readonly(self):
        def check(runtime,s):
            s.execute("CREATE TABLE t(id INT PRIMARY KEY,name TEXT)")
            s.execute("INSERT INTO t VALUES(1,'张三'),(2,'李四'),(3,'王五')")
            s.execute("CREATE VIEW v AS SELECT id,name FROM t WHERE id>=2")
            s.execute("CREATE VIEW v2 AS SELECT name AS student FROM v WHERE id=3")
            self.assertEqual(s.execute("SELECT * FROM v2").rows,[["王五"]])
            self.assertEqual(s.execute("SELECT a.id,b.name FROM t a JOIN v b ON a.id=b.id ORDER BY a.id").rows,[[2,"李四"],[3,"王五"]])
            self.assertEqual(s.execute("SELECT a.id FROM v a JOIN v b ON a.id=b.id ORDER BY a.id").rows,[[2],[3]])
            for query in ["UPDATE v SET name='bad'","INSERT INTO v VALUES(4,'bad')","DELETE FROM v","DROP VIEW v"]:
                with self.assertRaises(DBError): s.execute(query)
            s.execute("DROP VIEW v2")
            s.execute("DROP VIEW v")
            with self.assertRaises(DBError): s.execute("SELECT * FROM v")
        self.each_runtime(check)

    def test_view_restart_and_ddl_transaction_boundary(self):
        for cls in (Runtime,ContractRuntime):
            with self.subTest(runtime=cls.__name__),tempfile.TemporaryDirectory() as folder:
                with cls(folder) as runtime:
                    s=SqlSession(runtime)
                    s.execute("CREATE TABLE t(id INT)")
                    s.execute("INSERT INTO t VALUES(7)")
                    s.execute("CREATE VIEW v AS SELECT id FROM t")
                    s.execute("BEGIN")
                    with self.assertRaises(DBError): s.execute("DROP VIEW v")
                    s.execute("ROLLBACK")
                with cls(folder) as runtime:
                    self.assertEqual(SqlSession(runtime).execute("SELECT * FROM v").rows,[[7]])


class RequestedWorkbenchChanges(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.app=Workbench(self.root,"Administrator-test-123",True)
        self.token=self.app.login("admin","Administrator-test-123")

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_plain_classroom_seed_and_relationships(self):
        rows=self.app.execute(self.token,"SELECT id,name FROM students ORDER BY id")["rows"]
        self.assertEqual(len(rows),20)
        self.assertEqual(rows[:4],[[1,"张三"],[2,"李四"],[3,"王五"],[4,"李明"]])
        self.assertEqual(len({r[1] for r in rows}),20)
        self.assertEqual(self.app.execute(self.token,"SELECT COUNT(*) FROM enrollments")["rows"],[[30]])
        self.assertEqual(self.app.execute(self.token,"SELECT COUNT(DISTINCT course) FROM enrollments")["rows"],[[3]])
        self.assertNotIn("note",[c.name for c in self.app.runtime.storage.get_table("students").columns])

    def test_estimate_does_not_execute_target_query(self):
        with patch.object(self.app.runtime.executor,"execute",side_effect=AssertionError("target query executed")):
            estimate=self.app.estimate(self.token,"SELECT name FROM students WHERE id=5")
        self.assertEqual(estimate["phase"],"before_execution")
        self.assertEqual(estimate["off"]["rows"],estimate["on"]["rows"])
        self.assertGreater(estimate["off"]["operations"],estimate["on"]["operations"])
        self.assertIn("log",estimate["on"]["complexity"])
        self.assertNotIn("estimated_ms",estimate["on"])
        self.assertTrue(estimate["assumptions"])

    def test_compare_contains_pre_execution_prediction(self):
        report=self.app.compare(self.token,"SELECT name FROM students WHERE id=5",1)
        self.assertTrue(report["equivalent"])
        self.assertEqual(report["prediction"]["phase"],"before_execution")
        self.assertEqual(len(report["samples"]),2)

    def test_register_is_unprivileged_and_durable(self):
        self.app.register("newuser","Newuser-password-123")
        member=self.app.login("newuser","Newuser-password-123")
        self.assertEqual(self.app.state(member)["user"]["role"],"member")
        self.assertEqual(self.app.state(member)["tables"],[])
        self.app.close()
        self.app=Workbench(self.root)
        member=self.app.login("newuser","Newuser-password-123")
        self.assertEqual(self.app.state(member)["user"]["role"],"member")

    def test_view_permissions_require_view_and_underlying_table(self):
        self.app.execute(self.token,"CREATE VIEW student_names AS SELECT id,name FROM students")
        self.app.register("viewer","Viewer-password-123")
        member=self.app.login("viewer","Viewer-password-123")
        table=self.app.runtime.storage.get_table("students")
        view=self.app.runtime.storage.get_view("student_names")
        self.app.grant(self.token,"viewer",view.id,["SELECT"])
        with self.assertRaises(DBError): self.app.execute(member,"SELECT * FROM student_names")
        self.app.grant(self.token,"viewer",table.id,["SELECT"])
        self.assertEqual(len(self.app.execute(member,"SELECT * FROM student_names")["rows"]),20)
        self.app.grant(self.token,"viewer",view.id,[])
        with self.assertRaises(DBError): self.app.execute(member,"SELECT * FROM student_names")

    def test_accounts_pages_log_and_audit_are_ciphertext(self):
        self.app.execute(self.token,"INSERT INTO students VALUES(21,'敏感姓名测试Z9','计算机',88)")
        self.app.runtime.storage.flush_all()
        self.assertFalse((self.root/"security/users.json").exists())
        account=(self.root/"security/users.vault").read_bytes()
        self.assertTrue(account.startswith(b"EDENC1"))
        self.assertNotIn(b'"role"',account)
        files=list((self.root/"database/a_storage/pages").rglob("*.page"))+[self.root/"database/wal/contract.wal",self.root/"security/audit.vault"]
        self.assertTrue(files)
        for file in files:
            raw=file.read_bytes()
            self.assertNotIn("敏感姓名测试Z9".encode(),raw)
            self.assertNotIn(b"Administrator-test",raw)
        self.app.close()
        self.app=Workbench(self.root)
        token=self.app.login("admin","Administrator-test-123")
        self.assertEqual(self.app.execute(token,"SELECT name FROM students WHERE id=21")["rows"],[["敏感姓名测试Z9"]])

    def test_tampering_and_missing_identity_do_not_reset_admin(self):
        path=self.root/"security/users.vault"
        raw=path.read_bytes();changed=bytearray(raw);changed[-1]^=1
        path.write_bytes(changed)
        try:
            with self.assertRaises(DBError) as caught: IdentityStore(path,self.app.protector)
            self.assertEqual(caught.exception.code,"FILE_AUTHENTICATION_FAILED")
        finally: path.write_bytes(raw)
        self.app.close()
        path.unlink()
        with self.assertRaises(DBError) as caught: Workbench(self.root)
        self.assertEqual(caught.exception.code,"MIGRATION_REQUIRED")
        path.write_bytes(raw)

    def test_wrong_key_and_context_are_rejected(self):
        sealed=self.app.protector.seal(b"test-only",b"test")
        with tempfile.TemporaryDirectory() as second:
            other=FileProtector(second)
            with self.assertRaises(DBError): other.open(sealed,b"test")
        with self.assertRaises(DBError): self.app.protector.open(sealed,b"other")

    def test_http_registration_and_estimate_permissions(self):
        server=Server(("127.0.0.1",0),self.app)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        port=server.server_address[1]
        connection=http.client.HTTPConnection("127.0.0.1",port)
        self.addCleanup(connection.close)
        def post(path,payload):
            connection.request("POST",path,json.dumps(payload),{"Content-Type":"application/json","Origin":f"http://127.0.0.1:{port}"})
            response=connection.getresponse();status=response.status;response.read();return status
        self.assertEqual(post("/api/register",dict(username="eviluser",password="A-password-1234",role="admin")),403)
        self.assertEqual(post("/api/register",dict(username="validuser",password="A-password-1234")),201)
        self.assertEqual(post("/api/estimate",dict(sql="SELECT * FROM students")),401)


class RequestedMigration(unittest.TestCase):
    def test_wrapped_key_is_written_as_binary(self):
        # A Windows text-mode descriptor would turn LF into CRLF and corrupt
        # a random protected key. Force that byte rather than rely on chance.
        with tempfile.TemporaryDirectory() as folder:
            with patch("edudb.protection._dpapi",return_value=b"wrapped\nkey\r\n"):
                FileProtector(folder)
            self.assertEqual((Path(folder)/"security/key.dpapi").read_bytes(),b"wrapped\nkey\r\n")

    def test_encrypted_crash_and_interrupted_rollback(self):
        for mode in ("write","undo"):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                script='''
import os,sys
from edudb.workbench.service import Workbench
w=Workbench(sys.argv[1],'Crash-password-123',True)
t=w.login('admin','Crash-password-123')
w.execute(t,'BEGIN')
w.execute(t,"UPDATE students SET id=101,name='未提交内容' WHERE id=5")
w.runtime.storage.flush_all()
if sys.argv[2]=='undo':
 original=w.runtime.pages.apply_image
 def interrupted(key,image,lsn):
  original(key,image,lsn)
  if w.runtime.wal.get(lsn)['kind']=='CLR': os._exit(42)
 w.runtime.pages.apply_image=interrupted
 w.execute(t,'ROLLBACK')
os._exit(42)
'''
                process=subprocess.run([sys.executable,"-c",script,folder,mode],capture_output=True,text=True,timeout=30)
                self.assertEqual(process.returncode,42,process.stderr)
                for repeat in range(2):
                    app=Workbench(folder)
                    try:
                        token=app.login("admin","Crash-password-123")
                        self.assertEqual(app.execute(token,"SELECT name FROM students WHERE id=5")["rows"],[["赵六"]])
                        self.assertEqual(app.execute(token,"SELECT id FROM students WHERE id=101")["rows"],[])
                    finally: app.close()

    def test_closed_plaintext_database_copies_to_protected_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/"old";target=root/"new"
            with ContractRuntime(source/"database") as runtime:
                s=SqlSession(runtime)
                s.execute("CREATE TABLE t(id INT,name TEXT)")
                s.execute("INSERT INTO t VALUES(1,'旧数据保留')")
            salt=secrets.token_bytes(16)
            password="Legacy-password-123"
            data={"users":{"admin":dict(salt=salt.hex(),digest=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,600000).hex(),iterations=600000,role="admin",grants={})}}
            (source/"security").mkdir()
            (source/"security/users.json").write_text(json.dumps(data))
            before={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
            result=migrate(source,target)
            self.assertTrue(result["source_unchanged"])
            after={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
            self.assertEqual(before,after)
            app=Workbench(target)
            try:
                token=app.login("admin",password)
                self.assertEqual(app.execute(token,"SELECT * FROM t")["rows"],[[1,"旧数据保留"]])
            finally: app.close()
