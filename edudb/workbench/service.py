from __future__ import annotations

import hashlib
import json
import platform
import secrets
import statistics
import subprocess
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..c_api import DBError
from ..integration.contract_runtime import ContractRuntime
from ..sql import SqlSession, compile_sql
from ..sql.compiler import CompileOptions
from ..sql.parser import parse
from .audit import AuditLog, fingerprint, redact
from .security import IdentityStore
from .evidence import load_evidence, source_hash
from ..protection import FileProtector
from ..sql.estimates import estimate_plan


@dataclass
class Session:
    username: str
    csrf: str
    sql: SqlSession
    created: float
    touched: float
    history: list = field(default_factory=list)
    comparisons: list = field(default_factory=list)


class Workbench:
    def __init__(self, root, admin_password=None, demo=False, evidence_path=None):
        self.root = Path(root)
        existing=(self.root/"database"/"a_storage"/"storage.json").exists()
        if existing and not (self.root/"security"/"users.vault").exists():
            raise DBError("MIGRATION_REQUIRED","已有数据库缺少保护账号文件，请保留原目录并运行迁移工具")
        self.protector=FileProtector(self.root)
        self.runtime = ContractRuntime(self.root / "database", buffer_pages=16,protector=self.protector)
        self.lock = self.runtime.request_lock
        self.sessions = {}
        try:
            self.identities = IdentityStore(self.root / "security" / "users.vault",self.protector)
            self.audit = AuditLog(self.root / "security" / "audit.vault",self.protector)
            # Local classroom workflow: every server start announces a usable
            # admin password without rebuilding users, grants or database files.
            self.bootstrap_password = admin_password if admin_password is not None else secrets.token_urlsafe(16)
            if "admin" not in self.identities.data["users"]:
                self.identities.create("admin", self.bootstrap_password, "admin")
                self.audit.append("BOOTSTRAP", "admin")
            else:
                self.identities.reset_password("admin", self.bootstrap_password)
                self.audit.append("ADMIN_PASSWORD_ROTATED", "admin")
            self.evidence_path = evidence_path
            self.started = time.time()
            self.version = self._version()
            if demo and not self.runtime.storage.list_objects():
                self._seed()
        except BaseException:
            self.runtime.close()
            raise

    @staticmethod
    def _version():
        try:
            return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                           cwd=Path(__file__).resolve().parents[2], text=True, timeout=3).strip()
        except (OSError, subprocess.SubprocessError):
            return "unavailable"

    def _seed(self):
        sql = SqlSession(self.runtime)
        sql.execute("CREATE TABLE students (id INT PRIMARY KEY, name TEXT NOT NULL, department TEXT, score INT)")
        from ..demo_data import STUDENTS, ENROLLMENTS, sql_rows
        sql.execute("INSERT INTO students VALUES "+sql_rows(STUDENTS))
        sql.execute("CREATE INDEX idx_students_id ON students(id)")
        sql.execute("CREATE TABLE enrollments (student_id INT, course TEXT, credits INT)")
        sql.execute("INSERT INTO enrollments VALUES "+sql_rows(ENROLLMENTS))
        self.audit.append("SEED", "admin", dataset="classroom-v2", rows=len(STUDENTS)+len(ENROLLMENTS))

    def register(self,username,password,peer="local"):
        with self.lock:
            self.identities.create(username,password,"member")
            self.audit.append("REGISTER",username)
            return {"ok":True,"message":"账号已创建，请登录；数据访问权限由管理员授予"}

    def _drop(self, token):
        session = self.sessions.pop(token, None)
        if session and session.sql.active_tx_id and not self.runtime.transactions.needs_recovery:
            try:
                self.runtime.transactions.rollback(session.sql.active_tx_id)
            except DBError as exc:
                if exc.code != "INVALID_TRANSACTION":
                    raise

    def session(self, token):
        now = time.monotonic()
        for key, value in list(self.sessions.items()):
            if now - value.touched > 1800 or now - value.created > 8 * 3600:
                self._drop(key)
        if token not in self.sessions:
            raise DBError("UNAUTHENTICATED", "Please sign in")
        session = self.sessions[token]
        session.touched = now
        return session

    def login(self, username, password, peer="local"):
        with self.lock:
            now = time.monotonic()
            if not self.identities.verify(username, password):
                self.audit.append("LOGIN", None, False, error_code="INVALID_CREDENTIALS")
                raise DBError("INVALID_CREDENTIALS", "Invalid username or password")
            self.audit.append("LOGIN", username)
            token = secrets.token_urlsafe(32)
            self.sessions[token] = Session(username, secrets.token_urlsafe(32),
                                            SqlSession(self.runtime, user_id=username), now, now)
            return token

    def logout(self, token):
        with self.lock:
            user = self.session(token).username
            self._drop(token)
            self.audit.append("LOGOUT", user)

    def admin(self, session):
        if not self.identities.admin(session.username):
            raise DBError("FORBIDDEN", "Administrator permission required")

    def authorize(self, session, compiled):
        if compiled.kind in {"CREATE_TABLE", "CREATE_INDEX", "CREATE_VIEW", "DROP_VIEW"}:
            self.admin(session)
        for privilege in compiled.required_privileges:
            if privilege.action == "CREATE":
                self.admin(session)
                continue
            table = self.runtime.storage.get_view(privilege.object_id) if privilege.object_type=="VIEW" else self.runtime.storage.get_table(privilege.object_id)
            if not self.identities.allowed(session.username, table.id, privilege.action):
                raise DBError("FORBIDDEN", f"{privilege.action} denied on {table.name}")

    def _execute(self, session, sql, optimizer=True, teaching=True):
        if not isinstance(sql, str) or not sql.strip() or len(sql) > 32000:
            raise DBError("INVALID_SQL", "SQL must contain 1-32000 characters")
        started = time.perf_counter()
        compiled = compile_sql(sql, session.sql.catalog, CompileOptions(optimizer,teaching=teaching))
        planning_ms = (time.perf_counter() - started) * 1000
        self.authorize(session, compiled)
        if compiled.kind in {"CREATE_TABLE", "CREATE_INDEX", "CREATE_VIEW", "DROP_VIEW"} and session.sql.active_tx_id:
            raise DBError("DDL_TRANSACTION_CONFLICT", "Finish the current transaction before DDL")
        result = session.sql._dispatch(compiled)
        result.teaching = compiled.teaching
        if result.teaching and compiled.kind == "EXPLAIN":
            result.teaching["execution_kind"]="plan_only"
        result.metrics.planning_ms = planning_ms
        if compiled.kind == "EXPLAIN":
            result.plan = compiled.plan
        return result

    def execute(self, token, sql, optimizer=True):
        with self.lock:
            session = self.session(token)
            request = uuid.uuid4().hex
            safe = redact(sql) if isinstance(sql, str) else "[invalid]"
            digest = fingerprint(sql) if isinstance(sql, str) else None
            self.audit.append("SQL_REQUEST", session.username, request_id=request, sql_redacted=safe, sql_hash=digest)
            try:
                result = self._execute(session, sql, optimizer)
            except Exception as exc:
                self.audit.append("SQL_RESULT", session.username, False, request_id=request,
                                  error_code=getattr(exc, "code", "EXECUTION_ERROR"))
                raise
            response = asdict(result)
            total = len(response["rows"])
            response.update(rows=response["rows"][:500], total_rows=total, truncated=total > 500,
                            transaction_active=bool(session.sql.active_tx_id), request_id=request)
            self.audit.append("SQL_RESULT", session.username, request_id=request, metrics=response["metrics"])
            session.history.insert(0, dict(request_id=request, sql_redacted=safe, sql_hash=digest,
                                          metrics=response["metrics"], rows=total, time=time.time()))
            del session.history[30:]
            return response

    def state(self, token):
        with self.lock:
            session = self.session(token)
            tables = []
            for obj in self.runtime.storage.list_objects():
                if obj.kind not in {"TABLE","VIEW"} or not self.identities.allowed(session.username, obj.id, "SELECT"):
                    continue
                if obj.kind=="VIEW":
                    view=self.runtime.storage.get_view(obj.id)
                    tables.append(dict(id=view.id,name=view.name,kind="VIEW",columns=[dict(id=c.slot_id,name=c.name,type=c.type,nullable=c.nullable) for c in view.output_columns],indexes=[]))
                else:
                    table = self.runtime.storage.get_table(obj.id)
                    tables.append(dict(id=table.id, name=table.name,kind="TABLE",columns=[asdict(c) for c in table.columns],
                                       indexes=[asdict(i) for i in table.indexes]))
            diagnostics = self.runtime.storage.diagnostics()
            return dict(user=self.identities.public(session.username), csrf=session.csrf, tables=tables,
                        transaction_active=bool(session.sql.active_tx_id), history=session.history,
                        status="RECOVERY_REQUIRED" if self.runtime.transactions.needs_recovery else diagnostics["mode"],
                        version=self.version, buffer_capacity=16,
                        diagnostics=diagnostics if self.identities.admin(session.username) else None)

    @staticmethod
    def _bag(rows):
        return Counter(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)

    def estimate(self,token,sql):
        with self.lock:
            session=self.session(token)
            if self.runtime.transactions.has_active_transactions():
                raise DBError("EXPERIMENT_BUSY","请先结束所有活动事务")
            plans={}
            for mode,enabled in (("off",False),("on",True)):
                compiled=compile_sql(sql,session.sql.catalog,CompileOptions(enabled))
                if compiled.kind!="SELECT":
                    raise DBError("READ_ONLY_EXPERIMENT","仅能预估 SELECT 查询")
                self.authorize(session,compiled)
                plans[mode]=compiled.plan
            statistics={}
            for name in self.runtime.executor._tables(plans["off"]):
                table=self.runtime.storage.get_table(name)
                stat=self.runtime.storage.get_statistics(table.id)
                statistics[name]=dict(rows=stat.row_count,pages=stat.page_count,
                                      unique_columns=[c.name for c in table.columns if any(r.kind=="PRIMARY_KEY" and r.columns==[c.id] for r in table.constraints) or any(i.unique and i.column_ids==[c.id] for i in table.indexes)],
                                      unique_indexes=[i.id for i in table.indexes if i.unique or i.column_ids==[next((r.columns[0] for r in table.constraints if r.kind=="PRIMARY_KEY"),None)]])
            off=estimate_plan(plans["off"],statistics)
            on=estimate_plan(plans["on"],statistics)
            return dict(phase="before_execution",off=off,on=on,statistics=statistics,
                        assumptions=["使用实际表行数和页数；收集统计信息会读取数据页，但未执行目标查询", "普通过滤保留约三分之一，范围约四分之一，非唯一等值约十分之一；没有直方图", "计算量是行级操作的教学估算，不是毫秒预测；内存按保留行数估算", "相关子查询、数据倾斜和缓存差异可能使估算偏差增大"])

    def compare(self, token, sql, repeats=3, cache="cold"):
        with self.lock:
            session = self.session(token)
            if type(repeats) is not int or not 1 <= repeats <= 7 or cache not in {"cold", "warm"}:
                raise DBError("INVALID_EXPERIMENT", "Choose 1-7 runs and cold/warm cache")
            if self.runtime.transactions.has_active_transactions():
                raise DBError("EXPERIMENT_BUSY", "Finish all active transactions before comparing")
            compiled = compile_sql(sql, session.sql.catalog)
            if compiled.kind != "SELECT":
                raise DBError("READ_ONLY_EXPERIMENT", "COMPARE accepts SELECT only")
            self.authorize(session, compiled)
            prediction=self.estimate(token,sql)
            ordered = bool(parse(sql).order_by)
            self.audit.append("COMPARE_REQUEST", session.username, sql_redacted=redact(sql), sql_hash=fingerprint(sql))
            samples, plans, equivalent = [], {}, True
            try:
                for run in range(repeats):
                    results = {}
                    for enabled in ((False, True) if run % 2 == 0 else (True, False)):
                        mode = "on" if enabled else "off"
                        self.runtime.pages.reset_cache()
                        if cache == "warm":
                            self._execute(session, sql, enabled,teaching=False)
                        result = self._execute(session, sql, enabled,teaching=False)
                        results[mode] = result.rows
                        plans[mode] = asdict(result.plan)
                        metrics = asdict(result.metrics)
                        metrics["total_page_reads"] = metrics["data_page_reads"] + metrics["index_page_reads"]
                        samples.append(dict(run=run + 1, mode=mode, metrics=metrics))
                    equivalent &= (results["off"] == results["on"] if ordered else self._bag(results["off"]) == self._bag(results["on"]))
                keys = ("planning_ms", "execution_ms", "data_page_reads", "index_page_reads", "total_page_reads", "rows_returned")
                summary = {mode: {key: statistics.median(s["metrics"][key] for s in samples if s["mode"] == mode)
                                  for key in keys} for mode in ("off", "on")}
                report = dict(id=uuid.uuid4().hex, time=time.time(), sql_redacted=redact(sql), sql_hash=fingerprint(sql),
                              cache=cache, repeats=repeats, equivalent=equivalent, ordered=ordered,
                              comparison="sequence" if ordered else "multiset", samples=samples, summary=summary, plans=plans,
                              buffer_capacity=16, version=self.version,prediction=prediction)
                session.comparisons.insert(0, report)
                del session.comparisons[10:]
                self.audit.append("COMPARE_RESULT", session.username, equivalent, experiment_id=report["id"], summary=summary)
                return report
            except Exception as exc:
                self.audit.append("COMPARE_RESULT", session.username, False, error_code=getattr(exc, "code", "ERROR"))
                raise

    def users(self, token):
        with self.lock:
            session = self.session(token)
            self.admin(session)
            return [self.identities.public(name) for name in self.identities.data["users"]]

    def create_user(self, token, username, password, role):
        with self.lock:
            session = self.session(token)
            self.admin(session)
            self.audit.append("CREATE_USER_REQUEST", session.username)
            self.identities.create(username, password, role)
            self.audit.append("CREATE_USER", session.username, target=username, role=role)

    def grant(self, token, username, table_id, actions):
        with self.lock:
            session = self.session(token)
            self.admin(session)
            if self.runtime.transactions.has_active_transactions():
                raise DBError("ACTIVE_TRANSACTIONS", "Finish transactions before changing privileges")
            try:
                table = self.runtime.storage.get_table(table_id)
            except Exception:
                table = self.runtime.storage.get_view(table_id)
            self.audit.append("GRANT_REQUEST", session.username, target=username, table_id=table.id)
            self.identities.grant(username, table.id, actions)
            self.audit.append("GRANT", session.username, target=username, table_id=table.id, actions=actions)

    def events(self, token):
        with self.lock:
            session = self.session(token)
            self.admin(session)
            return self.audit.events[-200:][::-1]

    def report(self, token):
        with self.lock:
            session = self.session(token)
            self.audit.append("EXPORT_REPORT", session.username)
            def plan_shape(node):
                return dict(node_id=node["node_id"], op=node["op"],
                            children=[plan_shape(child) for child in node["children"]],
                            applied_rules=node.get("applied_rules", []))
            experiments = json.loads(json.dumps(session.comparisons))
            for experiment in experiments:
                experiment["plans"] = {mode: plan_shape(plan) for mode, plan in experiment["plans"].items()}
            return dict(format="edudb-workbench-report-v1", generated_at=time.time(), user=session.username,
                        environment=dict(python=platform.python_version(), platform=platform.platform(), commit=self.version, source_sha256=source_hash()),
                        tests=load_evidence(self.evidence_path),
                        queries=session.history, experiments=experiments,
                        audit_tail=self.audit.tail,
                        limitations=["Local coursework server; no TLS or production security certification",
                                     "Cold cache resets application buffers, not OS cache",
                                     "Ordered ties use conservative sequence comparison",
                                     "Query write counters exclude some background/catalog writes"])

    def close(self):
        with self.lock:
            for token in list(self.sessions):
                self._drop(token)
            self.runtime.close()
