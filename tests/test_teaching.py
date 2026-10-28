from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from edudb.c_api import DBError
from edudb.integration.runtime import Runtime
from edudb.integration.contract_runtime import ContractRuntime
from edudb.sql import SqlSession, compile_sql
from edudb.sql.compiler import CompileOptions
from edudb.sql.optimizer import Optimizer
from edudb.workbench.service import Workbench


def nodes(plan):
    yield plan
    for child in plan['children']: yield from nodes(child)


class TeachingPlanTests(unittest.TestCase):
    def each_runtime(self,check):
        for cls in (Runtime,ContractRuntime):
            with self.subTest(runtime=cls.__name__),tempfile.TemporaryDirectory() as folder:
                with cls(folder) as runtime:
                    s=SqlSession(runtime)
                    s.execute('CREATE TABLE t(id INT PRIMARY KEY,name TEXT,score INT)')
                    s.execute("INSERT INTO t VALUES(1,'张三',90),(2,'李四',50),(3,'王五',80),(4,'李明',NULL)")
                    s.execute('CREATE INDEX ix ON t(id)')
                    s.execute('CREATE VIEW v AS SELECT id AS sid,name,score FROM t WHERE score>=60')
                    check(runtime,s)

    def test_actual_view_pushdown_trace_is_a_continuous_sequence(self):
        def check(runtime,s):
            sql='SELECT a.name FROM v a WHERE a.sid=3'
            compiled=compile_sql(sql,s.catalog,CompileOptions(True,True))
            t=compiled.teaching
            self.assertGreaterEqual(len(t['steps']),4)
            self.assertIn('projection_pushdown',[step['rule'] for step in t['steps']])
            self.assertIn('filter_merge',[step['rule'] for step in t['steps']])
            self.assertIn('index_scan',[step['rule'] for step in t['steps']])
            previous=t['baseline']
            for step in t['steps']:
                self.assertEqual(step['before'],previous)
                self.assertNotEqual(step['before'],step['after'])
                self.assertTrue(step['reason'])
                previous=step['after']
            self.assertEqual(previous,asdict(compiled.plan))
            self.assertTrue(any(n['op']=='SeqScan' for n in nodes(t['baseline'])))
            self.assertTrue(any(n['op']=='IndexScan' for n in nodes(previous)))
            saved=deepcopy(t)
            compiled.plan.properties['changed_after_compile']=True
            self.assertEqual(t,saved)
            self.assertEqual(s.execute(sql,False).rows,[["王五"]])
            self.assertEqual(s.execute(sql,True).rows,[["王五"]])
        self.each_runtime(check)

    def test_nested_alias_lineage_and_repeated_references(self):
        def check(runtime,s):
            s.execute('CREATE VIEW v2 AS SELECT sid AS student_id,name FROM v')
            compiled=compile_sql('SELECT a.name FROM v2 a JOIN v2 b ON a.student_id=b.student_id WHERE a.student_id=3',s.catalog,CompileOptions(True,True))
            views=compiled.teaching['views']
            self.assertEqual(len(views),4)
            self.assertEqual(len({v['node_id'] for v in views}),4)
            self.assertEqual({v['alias'] for v in views if v['view_name']=='v2'},{'a','b'})
            self.assertTrue(all('t.name' in c['origins'] for v in views for c in v['columns'] if c['output']=='name'))
            self.assertEqual(s.execute('SELECT a.name FROM v2 a JOIN v2 b ON a.student_id=b.student_id WHERE a.student_id=3').rows,[["王五"]])
        self.each_runtime(check)

    def test_optimization_off_still_expands_views_without_steps(self):
        def check(runtime,s):
            t=compile_sql('SELECT name FROM v WHERE sid=1',s.catalog,CompileOptions(False,True)).teaching
            self.assertEqual(len(t['views']),1)
            self.assertEqual(t['steps'],[])
            self.assertEqual(t['step_count'],0)
            self.assertFalse(t['optimizer_enabled'])
        self.each_runtime(check)

    def test_trace_is_opt_in(self):
        self.each_runtime(lambda r,s:self.assertIsNone(compile_sql('SELECT * FROM v',s.catalog).teaching))

    def test_limit_distinct_aggregate_and_computed_projection_boundaries(self):
        def check(runtime,s):
            definitions=[
                ('limited','SELECT id FROM t ORDER BY id LIMIT 2','SELECT id FROM limited WHERE id=3',[]),
                ('distinct_ids','SELECT DISTINCT id FROM t','SELECT id FROM distinct_ids WHERE id=3',[[3]]),
                ('grouped','SELECT score,COUNT(*) AS n FROM t GROUP BY score','SELECT n FROM grouped WHERE score=90',[[1]]),
                ('computed','SELECT id,name,score+1 AS score2 FROM t','SELECT name FROM computed WHERE id=3',[["王五"]])]
            for name,definition,query,expected in definitions:
                s.execute('CREATE VIEW '+name+' AS '+definition)
                for enabled in (False,True): self.assertEqual(s.execute(query,enabled).rows,expected)
                plan=compile_sql(query,s.catalog,CompileOptions(True,True))
                self.assertFalse(any(n['op']=='IndexScan' for n in nodes(asdict(plan.plan))),query)
            s.execute('CREATE VIEW failing AS SELECT id,10/(id-id) AS bad FROM t')
            for enabled in (False,True):
                with self.assertRaises(DBError): s.execute('SELECT id FROM failing WHERE id=99',enabled)
        self.each_runtime(check)

    def test_outer_join_pushdown_matches_independent_reference(self):
        def check(runtime,s):
            s.execute('CREATE TABLE e(sid INT,course TEXT)')
            s.execute("INSERT INTO e VALUES(1,'数据库'),(1,'操作系统'),(2,'数据库'),(5,'网络')")
            db=sqlite3.connect(':memory:')
            try:
                db.executescript("CREATE TABLE t(id INT,name TEXT,score INT); INSERT INTO t VALUES(1,'张三',90),(2,'李四',50),(3,'王五',80),(4,'李明',NULL);CREATE TABLE e(sid INT,course TEXT);INSERT INTO e VALUES(1,'数据库'),(1,'操作系统'),(2,'数据库'),(5,'网络');")
                for kind in ('INNER','LEFT','RIGHT','FULL'):
                    for condition in ('t.id>=2','e.sid>=2','t.id=1 AND e.sid=1'):
                        sql=f'SELECT t.id,e.sid FROM t {kind} JOIN e ON t.id=e.sid WHERE {condition}'
                        expected=Counter(tuple(r) for r in db.execute(sql))
                        for enabled in (False,True): self.assertEqual(Counter(tuple(r) for r in s.execute(sql,enabled).rows),expected,(kind,condition,enabled))
            finally: db.close()
        self.each_runtime(check)

    def test_repeated_and_contradictory_view_conditions(self):
        def check(runtime,s):
            for condition in ('sid>2 AND sid>1','sid>=1 AND sid<3','sid>3 AND sid<2','sid=3 AND sid>3','sid=1 OR sid=3'):
                sql='SELECT name FROM v WHERE '+condition+' ORDER BY name'
                self.assertEqual(s.execute(sql,False).rows,s.execute(sql,True).rows)
        self.each_runtime(check)


class TeachingWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=Workbench(self.tmp.name,'Teaching-password-123',True)
        self.token=self.app.login('admin','Teaching-password-123')
        self.app.execute(self.token,'CREATE VIEW passed_students AS SELECT id,name,score FROM students WHERE score>=60')

    def tearDown(self):
        self.app.close();self.tmp.cleanup()

    def test_query_executes_once_and_returns_matching_real_metrics(self):
        executor=self.app.runtime.executor
        with patch.object(executor,'execute',wraps=executor.execute) as execute:
            result=self.app.execute(self.token,'SELECT name FROM passed_students WHERE id=5')
            self.assertEqual(execute.call_count,1)
        self.assertEqual(result['rows'],[["赵六"]])
        self.assertTrue(result['teaching']['views'])
        plan_ids={n['node_id'] for n in nodes(result['plan'])}
        self.assertTrue(set(result['metrics']['per_operator'])<=plan_ids)
        self.assertEqual(result['metrics']['per_operator'][result['plan']['node_id']]['output_rows'],1)
        self.assertEqual(result['teaching']['execution_kind'],'statistics_replay')

    def test_explain_has_no_fabricated_execution(self):
        with patch.object(self.app.runtime.executor,'execute',side_effect=AssertionError('must not execute')):
            result=self.app.execute(self.token,'EXPLAIN SELECT name FROM passed_students WHERE id=5')
        self.assertEqual(result['teaching']['execution_kind'],'plan_only')
        self.assertEqual(result['metrics']['per_operator'],{})
        self.assertTrue(result['teaching']['steps'])

    def test_unauthorized_view_trace_does_not_leak(self):
        self.app.create_user(self.token,'viewer','Viewer-password-123','member')
        view=self.app.runtime.storage.get_view('passed_students')
        self.app.grant(self.token,'viewer',view.id,['SELECT'])
        member=self.app.login('viewer','Viewer-password-123')
        for sql in ('SELECT * FROM passed_students','EXPLAIN SELECT * FROM passed_students'):
            with self.assertRaises(DBError) as caught: self.app.execute(member,sql)
            self.assertEqual(caught.exception.code,'FORBIDDEN')
        table=self.app.runtime.storage.get_table('students')
        self.app.grant(self.token,'viewer',table.id,['SELECT'])
        result=self.app.execute(member,'SELECT name FROM passed_students WHERE id=5')
        self.assertTrue(result['teaching']['views'])

    def test_comparison_does_not_collect_teaching_snapshots_in_timed_runs(self):
        with patch('edudb.workbench.service.compile_sql',wraps=compile_sql) as compile:
            report=self.app.compare(self.token,'SELECT name FROM passed_students WHERE id=5',1)
        self.assertTrue(report['equivalent'])
        self.assertTrue(all(not call.args[2].teaching for call in compile.call_args_list if len(call.args)>2))
        raw=json.dumps(self.app.report(self.token),ensure_ascii=False)
        self.assertNotIn('view_expansions',raw)
        self.assertNotIn('query_sql',raw)

    def test_ddl_has_no_stale_teaching_result(self):
        result=self.app.execute(self.token,'DROP VIEW passed_students')
        self.assertIsNone(result['teaching'])
