"""Top-level SQL compiler: text -> CompiledStatement for D's request path.

A CompiledStatement is a plain description of what to run. It never executes;
the driver (or D's authenticated handler) dispatches it against the C runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from copy import deepcopy

from ..c_api import DBError, IndexSpec, PlanNode, ResultColumn
from ..models import TableSchema
from . import ast
from .catalog import CatalogView
from .optimizer import optimize, Optimizer
from .parser import parse
from .planner import Planner, Privilege

_QUERY_KINDS = {ast.SelectStmt: "SELECT", ast.InsertStmt: "INSERT",
                ast.UpdateStmt: "UPDATE", ast.DeleteStmt: "DELETE"}


@dataclass
class CompileOptions:
    optimizer_enabled: bool = True
    teaching: bool = False


@dataclass
class CompiledStatement:
    kind: str                       # SELECT|INSERT|UPDATE|DELETE|CREATE_TABLE|CREATE_INDEX|BEGIN|COMMIT|ROLLBACK|EXPLAIN
    sql: str
    plan: PlanNode | None = None
    output_columns: list[ResultColumn] = field(default_factory=list)
    table_schema: TableSchema | None = None
    constraints: list = field(default_factory=list)
    index_spec: IndexSpec | None = None
    txn_action: str | None = None
    required_privileges: list[Privilege] = field(default_factory=list)
    optimized: bool = False
    explain: bool = False
    explain_text: str | None = None
    view_name: str | None = None
    view_sql: str | None = None
    view_dependencies: list = field(default_factory=list)
    teaching: dict | None = None


def compile_sql(sql: str, catalog: CatalogView, options: CompileOptions | None = None) -> CompiledStatement:
    options = options or CompileOptions()
    return _compile_node(parse(sql), sql, catalog, options)


def _compile_node(node, sql, catalog, options) -> CompiledStatement:
    if isinstance(node, ast.CreateViewStmt):
        if catalog.get_table(node.name) is not None or (getattr(catalog,"get_view",lambda n:None)(node.name) is not None):
            raise DBError("OBJECT_EXISTS", "同名表或视图已存在")
        planner = Planner(catalog)
        plan, columns = planner.plan_select(node.query)
        if len({c.name for c in columns}) != len(columns):
            raise DBError("DUPLICATE_COLUMN", "视图输出字段必须具有不同名称，请使用别名")
        requirements = planner.privileges + [Privilege("VIEW",node.name,"CREATE")]
        dependencies = sorted({(p.object_type,p.object_id) for p in planner.privileges})
        return CompiledStatement("CREATE_VIEW",sql,output_columns=columns,view_name=node.name,
                                 view_sql=node.query_sql,view_dependencies=dependencies,required_privileges=requirements)
    if isinstance(node, ast.DropViewStmt):
        view = getattr(catalog,"get_view",lambda n:None)(node.name)
        if view is None:
            raise DBError("OBJECT_NOT_FOUND", "视图不存在")
        return CompiledStatement("DROP_VIEW",sql,view_name=node.name,
                                 required_privileges=[Privilege("VIEW",node.name,"DROP")])
    if isinstance(node, ast.ExplainStmt):
        inner = _compile_node(node.inner, sql, catalog, options)
        if inner.plan is None:
            raise DBError("UNSUPPORTED_EXPLAIN", "EXPLAIN only supports SELECT/INSERT/UPDATE/DELETE")
        return CompiledStatement("EXPLAIN", sql, plan=inner.plan,
                                 output_columns=[ResultColumn("plan", "plan", "TEXT", False)],
                                 required_privileges=inner.required_privileges,
                                 optimized=inner.optimized, explain=True,
                                 explain_text=render_plan(inner.plan),teaching=inner.teaching)
    if type(node) in _QUERY_KINDS:
        planner = Planner(catalog)
        plan, columns = planner.plan_statement(node)
        teaching=None
        if options.teaching and isinstance(node,ast.SelectStmt):
            baseline=deepcopy(plan)
            optimizer=Optimizer(catalog,trace=True)
            if options.optimizer_enabled:
                plan=optimizer.run(plan)
            teaching=dict(views=planner.view_expansions,baseline=asdict(baseline),
                          steps=optimizer.steps,step_count=optimizer.step_count,
                          trace_truncated=optimizer.step_count>len(optimizer.steps),
                          optimizer_enabled=options.optimizer_enabled,
                          execution_kind="statistics_replay")
        elif options.optimizer_enabled:
            plan = optimize(plan, catalog)
        return CompiledStatement(_QUERY_KINDS[type(node)], sql, plan=plan, output_columns=columns,
                                 required_privileges=planner.privileges,
                                 optimized=options.optimizer_enabled,teaching=teaching)
    if isinstance(node, ast.CreateTableStmt):
        planner = Planner(catalog)
        schema, constraints = planner.plan_create_table(node)
        return CompiledStatement("CREATE_TABLE", sql, table_schema=schema, constraints=constraints,
                                 required_privileges=planner.privileges)
    if isinstance(node, ast.CreateIndexStmt):
        planner = Planner(catalog)
        spec = planner.plan_create_index(node)
        return CompiledStatement("CREATE_INDEX", sql, index_spec=spec,
                                 required_privileges=planner.privileges)
    if isinstance(node, ast.TxnStmt):
        return CompiledStatement(node.action, sql, txn_action=node.action)
    raise DBError("INTERNAL_ERROR", f"Cannot compile {type(node).__name__}")


# --- EXPLAIN rendering ---------------------------------------------------

def render_plan(node: PlanNode) -> str:
    return "\n".join(_render(node, 0))


def _render(node: PlanNode, indent: int) -> list[str]:
    lines = ["  " * indent + node.op + _suffix(node)]
    for child in node.children:
        lines += _render(child, indent + 1)
    return lines


def _suffix(node: PlanNode) -> str:
    parts = []
    props = node.properties
    if "table_id" in props:
        parts.append(f"table={props['table_id']}")
    if node.op == "IndexScan":
        parts.append(f"index={props.get('index_id')}")
        spec = props.get("range")
        if spec is not None:
            parts.append(f"range=[{spec.lower},{spec.upper}]")
    if "join_type" in props:
        parts.append(props["join_type"])
    if node.op == "HashJoin":
        parts.append("hashed")
    if node.op == "Limit":
        parts.append(f"limit={props.get('limit')} offset={props.get('offset', 0)}")
    if node.applied_rules:
        parts.append("rules=" + "+".join(node.applied_rules))
    return (" [" + ", ".join(parts) + "]") if parts else ""
