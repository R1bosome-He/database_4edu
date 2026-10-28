"""Binding and logical planning: ast.py + CatalogView -> C's c_api plan.

The planner resolves names to stable slot ids, enforces catalog/type rules, and
emits the *baseline* (optimizer-off) physical shape: SeqScan + NestedLoopJoin +
Filter. optimizer.py rewrites that into index scans / hash joins. All produced
objects are edudb.c_api dataclasses, i.e. exactly what QueryExecutor consumes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..c_api import Constraint, DBError, Expr, IndexSpec, PlanNode, RangeSpec, ResultColumn
from ..models import Column, TableSchema
from . import ast
from .catalog import CatalogView, TableInfo

AGG_NAMES = {"count", "sum", "avg", "min", "max"}
_NUMERIC = {"INT", "FLOAT"}


# --- binding scope -------------------------------------------------------

@dataclass(frozen=True)
class BoundColumn:
    alias: str | None
    name: str
    slot_id: str
    type: str
    nullable: bool


class Scope:
    def __init__(self, columns: list[BoundColumn], parent: "Scope | None" = None) -> None:
        self.columns = columns
        self.parent = parent

    def _match(self, table: str | None, name: str) -> list[BoundColumn]:
        return [c for c in self.columns
                if c.name == name and (table is None or c.alias == table)]

    def resolve(self, table: str | None, name: str, pos: int) -> tuple[BoundColumn, int]:
        scope, level = self, 0
        while scope is not None:
            matches = scope._match(table, name)
            if len(matches) == 1:
                return matches[0], level
            if len(matches) > 1:
                qualified = f"{table}.{name}" if table else name
                raise DBError("AMBIGUOUS_COLUMN", f"Column {qualified!r} is ambiguous", position=pos)
            scope, level = scope.parent, level + 1
        qualified = f"{table}.{name}" if table else name
        raise DBError("UNKNOWN_COLUMN", f"Unknown column {qualified!r}", position=pos)


@dataclass
class Bound:
    expr: Expr
    type: str            # INT | FLOAT | TEXT | BOOL | NULL
    nullable: bool = True


def _value_type(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT"
    if isinstance(value, float):
        return "FLOAT"
    return "TEXT"


def expr_key(expr: Expr):
    """Structural key so GROUP BY expressions can be matched by equality."""
    return (expr.kind, expr.operator, (type(expr.value).__name__, expr.value),
            expr.slot_id, expr.level, expr.distinct,
            tuple(expr_key(a) for a in expr.args),
            id(expr.plan) if expr.plan is not None else None)


@dataclass
class AggContext:
    input_scope: Scope
    group_slots: dict               # expr_key -> (slot, type, nullable)
    agg_slots: dict = field(default_factory=dict)    # expr_key -> (slot, type, nullable)
    agg_exprs: list = field(default_factory=list)    # c_api Expr per aggregate
    agg_cols: list = field(default_factory=list)     # ResultColumn per aggregate


# --- privileges exposed to D --------------------------------------------

@dataclass(frozen=True)
class Privilege:
    object_type: str     # TABLE | INDEX
    object_id: str
    action: str          # SELECT | INSERT | UPDATE | DELETE | CREATE


class Planner:
    def __init__(self, catalog: CatalogView) -> None:
        self.catalog = catalog
        self._counter = 0
        self.privileges: list[Privilege] = []
        self._view_stack = []
        self.view_expansions = []

    # --- id / helper ----------------------------------------------------
    def _nid(self, label: str) -> str:
        self._counter += 1
        return f"{label}{self._counter}"

    def _require_table(self, name: str, pos: int = 0) -> TableInfo:
        info = self.catalog.get_table(name)
        if info is None:
            if getattr(self.catalog,"get_view",lambda n:None)(name) is not None:
                raise DBError("READ_ONLY_VIEW","视图只允许查询，请修改原始表",position=pos)
            raise DBError("UNKNOWN_TABLE", f"Unknown table {name!r}", position=pos)
        return info

    def _need(self, object_type: str, object_id: str, action: str) -> None:
        self.privileges.append(Privilege(object_type, object_id, action))

    # --- public dispatch ------------------------------------------------
    def plan_statement(self, node: ast.Node):
        if isinstance(node, ast.SelectStmt):
            plan, cols = self.plan_select(node)
            return plan, cols
        if isinstance(node, ast.InsertStmt):
            return self.plan_insert(node), []
        if isinstance(node, ast.UpdateStmt):
            return self.plan_update(node), []
        if isinstance(node, ast.DeleteStmt):
            return self.plan_delete(node), []
        raise DBError("INTERNAL_ERROR", f"Planner cannot handle {type(node).__name__}")

    # --- SELECT ---------------------------------------------------------
    def plan_select(self, stmt: ast.SelectStmt, outer: Scope | None = None):
        if stmt.from_ is None:
            source = PlanNode(self._nid("values"), "Values", output_columns=[], properties={"rows": [[]]})
            input_scope = Scope([], outer)
        else:
            source, columns = self.plan_from(stmt.from_, outer)
            input_scope = Scope(columns, outer)

        if stmt.where is not None:
            cond = self._bind_bool(stmt.where, input_scope, "WHERE", allow_agg=False)
            source = PlanNode(self._nid("filter"), "Filter", [source],
                              list(source.output_columns), [cond.expr])

        aggregated = bool(stmt.group_by) or self._has_aggregate(stmt)
        if aggregated:
            return self._plan_aggregated(stmt, source, input_scope)
        return self._plan_simple(stmt, source, input_scope)

    def _has_aggregate(self, stmt: ast.SelectStmt) -> bool:
        candidates = [item.expr for item in stmt.select_list]
        candidates += [o.expr for o in stmt.order_by]
        if stmt.having is not None:
            candidates.append(stmt.having)
        return any(contains_aggregate(node) for node in candidates)

    def _plan_simple(self, stmt, source, input_scope):
        out_cols, out_exprs, alias_exprs = self._bind_select_list(stmt.select_list, input_scope)
        node = source
        if stmt.distinct:
            node = PlanNode(self._nid("proj"), "Projection", [node], out_cols, out_exprs)
            node = PlanNode(self._nid("distinct"), "Distinct", [node], list(out_cols))
            if stmt.order_by:
                node = self._sort_over_outputs(stmt.order_by, node, out_cols)
        else:
            if stmt.order_by:
                node = self._sort_over_source(stmt.order_by, node, input_scope, out_exprs, alias_exprs)
            node = PlanNode(self._nid("proj"), "Projection", [node], out_cols, out_exprs)
        node = self._apply_limit(stmt, node, out_cols)
        return node, out_cols

    def _plan_aggregated(self, stmt, source, input_scope):
        agg = AggContext(input_scope, {})
        group_exprs, group_cols = [], []
        for i, g in enumerate(stmt.group_by):
            if contains_aggregate(g):
                raise DBError("GROUPING_ERROR", "GROUP BY cannot contain an aggregate")
            b = self._bind(g, input_scope)
            slot = f"g{i}"
            agg.group_slots[expr_key(b.expr)] = (slot, b.type, b.nullable)
            group_exprs.append(b.expr)
            group_cols.append(ResultColumn(slot, self._column_label(g, i, "group"), b.type, b.nullable))

        select_out = [(item, self._bind_grouped(item.expr, agg)) for item in stmt.select_list
                      if not isinstance(item.expr, ast.Star)]
        if len(select_out) != len(stmt.select_list):
            raise DBError("GROUPING_ERROR", "SELECT * is not allowed with GROUP BY/aggregates")
        having_expr = None
        if stmt.having is not None:
            having_expr = self._bind_grouped(stmt.having, agg, require_bool="HAVING").expr

        agg_node = PlanNode(self._nid("agg"), "Aggregate", [source],
                            group_cols + agg.agg_cols, group_exprs + agg.agg_exprs,
                            {"group_by": group_exprs})
        node = agg_node
        if having_expr is not None:
            node = PlanNode(self._nid("having"), "Filter", [node], list(agg_node.output_columns), [having_expr])

        out_cols, out_exprs, alias_exprs = [], [], {}
        for index, (item, bound) in enumerate(select_out):
            slot = self._nid("o")
            name = item.alias or self._column_label(item.expr, index, "col")
            out_cols.append(ResultColumn(slot, name, bound.type, bound.nullable))
            out_exprs.append(bound.expr)
            if item.alias:
                alias_exprs[item.alias] = bound

        if stmt.order_by:
            node = self._sort_over_source(stmt.order_by, node, input_scope, out_exprs, alias_exprs, agg=agg)
        node = PlanNode(self._nid("proj"), "Projection", [node], out_cols, out_exprs)
        if stmt.distinct:
            node = PlanNode(self._nid("distinct"), "Distinct", [node], list(out_cols))
        node = self._apply_limit(stmt, node, out_cols)
        return node, out_cols

    def _apply_limit(self, stmt, node, out_cols):
        if stmt.limit is not None or stmt.offset:
            node = PlanNode(self._nid("limit"), "Limit", [node], list(out_cols),
                            properties={"limit": stmt.limit, "offset": stmt.offset})
        return node

    # --- ORDER BY -------------------------------------------------------
    def _order_entry(self, item: ast.OrderItem, expr: Expr) -> dict:
        return {"expr": expr, "descending": item.descending,
                "nulls_first": bool(item.nulls_first)}

    def _sort_over_source(self, order_by, child, input_scope, select_exprs, alias_exprs, agg=None):
        entries = []
        for item in order_by:
            expr = self._order_key(item.expr, input_scope, select_exprs, alias_exprs, agg)
            entries.append(self._order_entry(item, expr))
        return PlanNode(self._nid("sort"), "Sort", [child], list(child.output_columns),
                        properties={"order_by": entries})

    def _order_key(self, node, input_scope, select_exprs, alias_exprs, agg):
        if isinstance(node, ast.Literal) and isinstance(node.value, int) and not isinstance(node.value, bool):
            if not 1 <= node.value <= len(select_exprs):
                raise DBError("INVALID_ORDER", f"ORDER BY position {node.value} is out of range", position=node.pos)
            return select_exprs[node.value - 1]
        if isinstance(node, ast.ColumnName) and node.table is None and node.name in alias_exprs:
            return alias_exprs[node.name].expr
        if agg is not None:
            return self._bind_grouped(node, agg).expr
        return self._bind(node, input_scope).expr

    def _sort_over_outputs(self, order_by, child, out_cols):
        by_name = {}
        for col in out_cols:
            by_name.setdefault(col.name, col.slot_id)
        entries = []
        for item in order_by:
            node = item.expr
            if isinstance(node, ast.Literal) and isinstance(node.value, int) and not isinstance(node.value, bool):
                if not 1 <= node.value <= len(out_cols):
                    raise DBError("INVALID_ORDER", f"ORDER BY position {node.value} is out of range", position=node.pos)
                slot = out_cols[node.value - 1].slot_id
            elif isinstance(node, ast.ColumnName) and node.table is None and node.name in by_name:
                slot = by_name[node.name]
            else:
                raise DBError("INVALID_ORDER", "ORDER BY under DISTINCT must reference an output column",
                              position=getattr(node, "pos", 0))
            entries.append(self._order_entry(item, Expr("ColumnRef", slot_id=slot)))
        return PlanNode(self._nid("sort"), "Sort", [child], list(child.output_columns),
                        properties={"order_by": entries})

    # --- SELECT list ----------------------------------------------------
    def _bind_select_list(self, select_list, scope):
        out_cols, out_exprs, alias_exprs = [], [], {}
        for index, item in enumerate(select_list):
            if isinstance(item.expr, ast.Star):
                for col in self._expand_star(item.expr, scope):
                    slot = self._nid("o")
                    out_cols.append(ResultColumn(slot, col.name, col.type, col.nullable))
                    out_exprs.append(Expr("ColumnRef", slot_id=col.slot_id))
                continue
            bound = self._bind(item.expr, scope)
            slot = self._nid("o")
            name = item.alias or self._column_label(item.expr, index, "col")
            out_cols.append(ResultColumn(slot, name, bound.type, bound.nullable))
            out_exprs.append(bound.expr)
            if item.alias:
                alias_exprs[item.alias] = bound
            elif isinstance(item.expr, ast.ColumnName):
                alias_exprs.setdefault(item.expr.name, bound)
        if len({c.slot_id for c in out_cols}) != len(out_cols):
            raise DBError("INTERNAL_ERROR", "Duplicate output slot generated")
        return out_cols, out_exprs, alias_exprs

    def _expand_star(self, star: ast.Star, scope: Scope):
        columns = [c for c in scope.columns if star.table is None or c.alias == star.table]
        if not columns:
            target = star.table or "*"
            raise DBError("UNKNOWN_TABLE", f"Nothing to expand for {target}.*", position=star.pos)
        return columns

    def _column_label(self, node, index, prefix):
        if isinstance(node, ast.ColumnName):
            return node.name
        if isinstance(node, ast.FuncCall):
            return node.name
        return f"{prefix}{index + 1}"

    # --- FROM -----------------------------------------------------------
    def plan_from(self, node, outer):
        if isinstance(node, ast.TableRef):
            return self._plan_table(node)
        if isinstance(node, ast.JoinRef):
            left, left_cols = self.plan_from(node.left, outer)
            right, right_cols = self.plan_from(node.right, outer)
            combined = left_cols + right_cols
            if len({c.slot_id for c in combined}) != len(combined):
                raise DBError("DUPLICATE_ALIAS", "A table alias is used more than once in FROM")
            join_type = "INNER" if node.join_type == "CROSS" else node.join_type
            expressions = []
            if node.on is not None:
                join_scope = Scope(combined, outer)
                expressions = [self._bind_bool(node.on, join_scope, "ON", allow_agg=False).expr]
            out_cols = list(left.output_columns) + list(right.output_columns)
            plan = PlanNode(self._nid("join"), "NestedLoopJoin", [left, right], out_cols,
                            expressions, {"join_type": join_type})
            return plan, combined
        raise DBError("INTERNAL_ERROR", "Unknown FROM node")

    def _plan_table(self, ref: ast.TableRef):
        getter = getattr(self.catalog, "get_view", None)
        view = getter(ref.name) if getter else None
        if view is not None:
            if ref.name in self._view_stack or len(self._view_stack) >= 32:
                raise DBError("VIEW_CYCLE", "视图依赖循环或嵌套过深")
            from .parser import parse
            self._need("VIEW", view.name, "SELECT")
            parent=self._view_stack[-1] if self._view_stack else None
            depth=len(self._view_stack)
            self._view_stack.append(ref.name)
            try:
                inner, outputs = self.plan_select(parse(view.query_sql))
            finally:
                self._view_stack.pop()
            alias = ref.alias or view.name
            columns = [BoundColumn(alias,c.name,f"{alias}.{c.name}",c.type,c.nullable) for c in outputs]
            out = [ResultColumn(c.slot_id,c.name,c.type,c.nullable) for c in columns]
            node = PlanNode(self._nid("view"), "Projection", [inner], out,
                            [Expr("ColumnRef",slot_id=c.slot_id) for c in outputs],
                            {"view_name":view.name,"view_alias":alias})
            self.view_expansions.append(dict(view_id=view.id,view_name=view.name,alias=alias,parent=parent,depth=depth,query_sql=view.query_sql,node_id=node.node_id,
                columns=[dict(output=c.name,slot=c.slot_id,source=source.slot_id,origins=_column_sources(inner,source.slot_id)) for c,source in zip(columns,outputs)]))
            return node, columns
        info = self._require_table(ref.name, ref.pos)
        alias = ref.alias or info.name
        self._need("TABLE", info.name, "SELECT")
        columns, out_cols = [], []
        for col in info.columns:
            slot = f"{alias}.{col.name}"
            columns.append(BoundColumn(alias, col.name, slot, col.type, col.nullable))
            out_cols.append(ResultColumn(slot, col.name, col.type, col.nullable))
        plan = PlanNode(self._nid("scan"), "SeqScan", output_columns=out_cols,
                        properties={"table_id": info.name, "columns": [c.name for c in info.columns]})
        return plan, columns

    # --- expression binding ---------------------------------------------
    def _bind_bool(self, node, scope, clause, allow_agg):
        if not allow_agg and contains_aggregate(node):
            raise DBError("GROUPING_ERROR", f"{clause} cannot contain an aggregate", position=getattr(node, "pos", 0))
        bound = self._bind(node, scope)
        if bound.type not in {"BOOL", "NULL"}:
            raise DBError("TYPE_ERROR", f"{clause} requires a boolean expression", position=getattr(node, "pos", 0))
        return bound

    def _bind(self, node, scope: Scope) -> Bound:
        if isinstance(node, ast.Literal):
            return Bound(Expr("Literal", node.value), _value_type(node.value), node.value is None)
        if isinstance(node, ast.ColumnName):
            col, level = scope.resolve(node.table, node.name, node.pos)
            return Bound(Expr("ColumnRef", slot_id=col.slot_id, level=level), col.type, col.nullable)
        if isinstance(node, ast.Star):
            raise DBError("SYNTAX_ERROR", "'*' is only valid in SELECT or COUNT(*)", position=node.pos)
        if isinstance(node, ast.UnaryOp):
            return self._bind_unary(node, scope)
        if isinstance(node, ast.BinaryOp):
            return self._bind_binary(node, scope)
        if isinstance(node, ast.IsNull):
            operand = self._bind(node.operand, scope)
            operator = "IS NOT NULL" if node.negated else "IS NULL"
            return Bound(Expr("IsNull", operator=operator, args=[operand.expr]), "BOOL", False)
        if isinstance(node, ast.Between):
            return self._bind_between(node, scope)
        if isinstance(node, ast.InList):
            return self._bind_in_list(node, scope)
        if isinstance(node, ast.InSubquery):
            return self._bind_in_subquery(node, scope)
        if isinstance(node, ast.ExistsExpr):
            sub, _ = self.plan_select(node.subquery, scope)
            operator = "NOT EXISTS" if node.negated else "EXISTS"
            return Bound(Expr("Subquery", operator=operator, plan=sub), "BOOL", False)
        if isinstance(node, ast.ScalarSubquery):
            sub, cols = self.plan_select(node.subquery, scope)
            if len(cols) != 1:
                raise DBError("SUBQUERY_WIDTH", "Scalar subquery must return exactly one column", position=node.pos)
            return Bound(Expr("Subquery", operator="SCALAR", plan=sub), cols[0].type, True)
        if isinstance(node, ast.FuncCall):
            if node.name.lower() not in AGG_NAMES:
                raise DBError("UNKNOWN_FUNCTION", f"Unknown function {node.name}()", position=node.pos)
            raise DBError("GROUPING_ERROR",
                          f"Aggregate {node.name}() is only valid in a grouped/aggregated query", position=node.pos)
        raise DBError("INTERNAL_ERROR", f"Cannot bind {type(node).__name__}")

    def _bind_unary(self, node, scope):
        if node.op == "-" and isinstance(node.operand, ast.Literal) and type(node.operand.value) is int:
            value = -node.operand.value
            if not -(2**63) <= value < 2**63:
                raise DBError("NUMERIC_OVERFLOW", "整数超出有符号 64 位范围", position=node.pos)
            return Bound(Expr("Literal", value), "INT", False)
        operand = self._bind(node.operand, scope)
        if node.op == "NOT":
            if operand.type not in {"BOOL", "NULL"}:
                raise DBError("TYPE_ERROR", "NOT requires a boolean operand", position=node.pos)
            return Bound(Expr("Unary", operator="NOT", args=[operand.expr]), "BOOL", True)
        if operand.type not in _NUMERIC | {"NULL"}:
            raise DBError("TYPE_ERROR", "Unary +/- requires a numeric operand", position=node.pos)
        return Bound(Expr("Unary", operator=node.op, args=[operand.expr]), operand.type, True)

    def _bind_binary(self, node, scope):
        if node.op in {"AND", "OR"}:
            left = self._bind(node.left, scope)
            right = self._bind(node.right, scope)
            for side in (left, right):
                if side.type not in {"BOOL", "NULL"}:
                    raise DBError("TYPE_ERROR", f"{node.op} requires boolean operands", position=node.pos)
            return Bound(Expr("Binary", operator=node.op, args=[left.expr, right.expr]), "BOOL", True)
        left = self._bind(node.left, scope)
        right = self._bind(node.right, scope)
        if node.op in {"=", "<>", "!=", "<", "<=", ">", ">="}:
            self._check_comparable(left, right, node.pos)
            return Bound(Expr("Binary", operator=node.op, args=[left.expr, right.expr]), "BOOL", True)
        # arithmetic
        for side in (left, right):
            if side.type not in _NUMERIC | {"NULL"}:
                raise DBError("TYPE_ERROR", "Arithmetic requires numeric operands", position=node.pos)
        result = "FLOAT" if (node.op == "/" or "FLOAT" in {left.type, right.type}) else "INT"
        if "NULL" in {left.type, right.type} and node.op != "/":
            result = "FLOAT" if "FLOAT" in {left.type, right.type} else "INT"
        return Bound(Expr("Binary", operator=node.op, args=[left.expr, right.expr]), result, True)

    def _check_comparable(self, left, right, pos):
        a, b = left.type, right.type
        if "NULL" in {a, b}:
            return
        if a in _NUMERIC and b in _NUMERIC:
            return
        if a != b:
            raise DBError("TYPE_ERROR", f"Cannot compare {a} with {b}", position=pos)

    def _bind_between(self, node, scope):
        operand = self._bind(node.operand, scope)
        low = self._bind(node.low, scope)
        high = self._bind(node.high, scope)
        self._check_comparable(operand, low, node.pos)
        self._check_comparable(operand, high, node.pos)
        if node.negated:
            lower = Expr("Binary", operator="<", args=[operand.expr, low.expr])
            upper = Expr("Binary", operator=">", args=[operand.expr, high.expr])
            return Bound(Expr("Binary", operator="OR", args=[lower, upper]), "BOOL", True)
        lower = Expr("Binary", operator=">=", args=[operand.expr, low.expr])
        upper = Expr("Binary", operator="<=", args=[operand.expr, high.expr])
        return Bound(Expr("Binary", operator="AND", args=[lower, upper]), "BOOL", True)

    def _bind_in_list(self, node, scope):
        operand = self._bind(node.operand, scope)
        equalities = []
        for item in node.items:
            value = self._bind(item, scope)
            self._check_comparable(operand, value, node.pos)
            equalities.append(Expr("Binary", operator="=", args=[operand.expr, value.expr]))
        disjunction = equalities[0]
        for eq in equalities[1:]:
            disjunction = Expr("Binary", operator="OR", args=[disjunction, eq])
        if node.negated:
            disjunction = Expr("Unary", operator="NOT", args=[disjunction])
        return Bound(disjunction, "BOOL", True)

    def _bind_in_subquery(self, node, scope):
        operand = self._bind(node.operand, scope)
        sub, cols = self.plan_select(node.subquery, scope)
        if len(cols) != 1:
            raise DBError("SUBQUERY_WIDTH", "IN subquery must return exactly one column", position=node.pos)
        operator = "NOT IN" if node.negated else "IN"
        return Bound(Expr("Subquery", operator=operator, args=[operand.expr], plan=sub), "BOOL", True)

    # --- grouped binding (aggregates + GROUP BY substitution) -----------
    def _bind_grouped(self, node, agg: AggContext, require_bool: str | None = None) -> Bound:
        bound = self._bind_grouped_inner(node, agg)
        if require_bool and bound.type not in {"BOOL", "NULL"}:
            raise DBError("TYPE_ERROR", f"{require_bool} requires a boolean expression", position=getattr(node, "pos", 0))
        return bound

    def _bind_grouped_inner(self, node, agg: AggContext) -> Bound:
        if isinstance(node, ast.FuncCall):
            return self._bind_aggregate(node, agg)
        # Whole-expression match against a GROUP BY key.
        if not contains_aggregate(node) and not _is_subquery(node):
            plain = self._bind(node, agg.input_scope)
            key = expr_key(plain.expr)
            if key in agg.group_slots:
                slot, typ, nul = agg.group_slots[key]
                return Bound(Expr("ColumnRef", slot_id=slot), typ, nul)
            if isinstance(node, ast.ColumnName):
                raise DBError("GROUPING_ERROR",
                              f"Column {node.name!r} must appear in GROUP BY or an aggregate", position=node.pos)
        return self._rebuild_grouped(node, agg)

    def _rebuild_grouped(self, node, agg) -> Bound:
        if isinstance(node, ast.Literal):
            return self._bind(node, agg.input_scope)
        if isinstance(node, ast.UnaryOp):
            operand = self._bind_grouped_inner(node.operand, agg)
            if node.op == "NOT":
                return Bound(Expr("Unary", operator="NOT", args=[operand.expr]), "BOOL", True)
            return Bound(Expr("Unary", operator=node.op, args=[operand.expr]), operand.type, True)
        if isinstance(node, ast.BinaryOp):
            left = self._bind_grouped_inner(node.left, agg)
            right = self._bind_grouped_inner(node.right, agg)
            if node.op in {"AND", "OR"}:
                return Bound(Expr("Binary", operator=node.op, args=[left.expr, right.expr]), "BOOL", True)
            if node.op in {"=", "<>", "!=", "<", "<=", ">", ">="}:
                return Bound(Expr("Binary", operator=node.op, args=[left.expr, right.expr]), "BOOL", True)
            result = "FLOAT" if (node.op == "/" or "FLOAT" in {left.type, right.type}) else "INT"
            return Bound(Expr("Binary", operator=node.op, args=[left.expr, right.expr]), result, True)
        if isinstance(node, ast.IsNull):
            operand = self._bind_grouped_inner(node.operand, agg)
            operator = "IS NOT NULL" if node.negated else "IS NULL"
            return Bound(Expr("IsNull", operator=operator, args=[operand.expr]), "BOOL", False)
        if _is_subquery(node):
            return self._bind(node, agg.input_scope)
        raise DBError("GROUPING_ERROR", "Expression is not valid in a grouped query",
                      position=getattr(node, "pos", 0))

    def _bind_aggregate(self, node: ast.FuncCall, agg: AggContext) -> Bound:
        name = node.name.lower()
        if name not in AGG_NAMES:
            raise DBError("UNKNOWN_FUNCTION", f"Unknown function {node.name}()", position=node.pos)
        if node.star:
            if name != "count" or node.distinct:
                raise DBError("TYPE_ERROR", "Only COUNT(*) accepts '*'", position=node.pos)
            expr = Expr("Aggregate", operator="COUNT")
            result_type, nullable = "INT", False
        else:
            if len(node.args) != 1:
                raise DBError("TYPE_ERROR", f"{node.name}() takes exactly one argument", position=node.pos)
            if contains_aggregate(node.args[0]):
                raise DBError("GROUPING_ERROR", "Aggregates cannot be nested", position=node.pos)
            arg = self._bind(node.args[0], agg.input_scope)
            if name in {"sum", "avg"} and arg.type not in _NUMERIC | {"NULL"}:
                raise DBError("TYPE_ERROR", f"{node.name}() requires a numeric argument", position=node.pos)
            expr = Expr("Aggregate", operator=name.upper(), args=[arg.expr], distinct=node.distinct)
            result_type, nullable = self._aggregate_type(name, arg.type)
        key = expr_key(expr)
        if key not in agg.agg_slots:
            slot = f"a{len(agg.agg_exprs)}"
            agg.agg_slots[key] = (slot, result_type, nullable)
            agg.agg_exprs.append(expr)
            agg.agg_cols.append(ResultColumn(slot, name, result_type, nullable))
        slot, typ, nul = agg.agg_slots[key]
        return Bound(Expr("ColumnRef", slot_id=slot), typ, nul)

    def _aggregate_type(self, name, arg_type):
        if name == "count":
            return "INT", False
        if name == "avg":
            return "FLOAT", True
        if name == "sum":
            return ("FLOAT" if arg_type == "FLOAT" else "INT"), True
        return (arg_type if arg_type != "NULL" else "NULL"), True   # MIN / MAX

    # --- INSERT / UPDATE / DELETE ---------------------------------------
    def plan_insert(self, stmt: ast.InsertStmt) -> PlanNode:
        info = self._require_table(stmt.table)
        self._need("TABLE", info.name, "INSERT")
        names = [c.name for c in info.columns]
        if stmt.columns is None:
            target = names
        else:
            target = stmt.columns
            for column in target:
                if column not in names:
                    raise DBError("UNKNOWN_COLUMN", f"Unknown column {column!r} in INSERT")
            if len(set(target)) != len(target):
                raise DBError("DUPLICATE_COLUMN", "Column named twice in INSERT")
        rows = []
        for row in stmt.rows:
            if len(row) != len(target):
                raise DBError("INSERT_ARITY", "INSERT column/value count mismatch")
            rows.append({column: self._const_value(value) for column, value in zip(target, row)})
        return PlanNode(self._nid("insert"), "Insert", properties={"table_id": info.name, "rows": rows})

    def _const_value(self, node):
        if isinstance(node, ast.Literal):
            return node.value
        if isinstance(node, ast.UnaryOp) and node.op in {"+", "-"}:
            inner = self._const_value(node.operand)
            if not isinstance(inner, (int, float)) or isinstance(inner, bool):
                raise DBError("TYPE_ERROR", "Unary +/- requires a number", position=node.pos)
            return inner if node.op == "+" else -inner
        if isinstance(node, ast.BinaryOp) and node.op in {"+", "-", "*", "/", "%"}:
            left, right = self._const_value(node.left), self._const_value(node.right)
            if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in (left, right)):
                raise DBError("TYPE_ERROR", "Constant arithmetic requires numbers", position=node.pos)
            if node.op in {"/", "%"} and right == 0:
                raise DBError("DIVISION_BY_ZERO", "Division by zero in INSERT value", position=node.pos)
            return {"+": lambda: left + right, "-": lambda: left - right, "*": lambda: left * right,
                    "/": lambda: left / right, "%": lambda: left % right}[node.op]()
        raise DBError("NON_CONSTANT_VALUE", "INSERT values must be constant expressions",
                      position=getattr(node, "pos", 0))

    def _dml_scope(self, info: TableInfo):
        columns = [BoundColumn(info.name, c.name, f"{info.name}.{c.name}", c.type, c.nullable)
                   for c in info.columns]
        input_columns = [ResultColumn(c.slot_id, c.name, c.type, c.nullable) for c in columns]
        return Scope(columns), input_columns, [c.name for c in info.columns]

    def plan_update(self, stmt: ast.UpdateStmt) -> PlanNode:
        info = self._require_table(stmt.table)
        self._need("TABLE", info.name, "UPDATE")
        scope, input_columns, column_names = self._dml_scope(info)
        assignments, seen = {}, set()
        for column, value in stmt.assignments:
            target = info.column(column)
            if target is None:
                raise DBError("UNKNOWN_COLUMN", f"Unknown column {column!r} in SET")
            if column in seen:
                raise DBError("DUPLICATE_COLUMN", f"Column {column!r} assigned twice")
            seen.add(column)
            bound = self._bind(value, scope)
            self._check_assignable(target, bound, getattr(value, "pos", 0))
            assignments[column] = bound.expr
        expressions = []
        if stmt.where is not None:
            expressions = [self._bind_bool(stmt.where, scope, "WHERE", allow_agg=False).expr]
        return PlanNode(self._nid("update"), "Update", expressions=expressions,
                        properties={"table_id": info.name, "input_columns": input_columns,
                                    "columns": column_names, "assignments": assignments})

    def plan_delete(self, stmt: ast.DeleteStmt) -> PlanNode:
        info = self._require_table(stmt.table)
        self._need("TABLE", info.name, "DELETE")
        scope, input_columns, column_names = self._dml_scope(info)
        expressions = []
        if stmt.where is not None:
            expressions = [self._bind_bool(stmt.where, scope, "WHERE", allow_agg=False).expr]
        return PlanNode(self._nid("delete"), "Delete", expressions=expressions,
                        properties={"table_id": info.name, "input_columns": input_columns,
                                    "columns": column_names})

    def _check_assignable(self, column, bound, pos):
        if bound.type == "NULL":
            return
        if column.type in _NUMERIC and bound.type in _NUMERIC:
            return
        if column.type != bound.type:
            raise DBError("TYPE_ERROR",
                          f"Cannot assign {bound.type} to {column.type} column {column.name!r}", position=pos)

    # --- DDL ------------------------------------------------------------
    def plan_create_table(self, stmt: ast.CreateTableStmt):
        if self.catalog.get_table(stmt.name) is not None:
            raise DBError("TABLE_EXISTS", f"Table {stmt.name!r} already exists")
        names = [c.name for c in stmt.columns]
        if len(set(names)) != len(names):
            raise DBError("DUPLICATE_COLUMN", "Column names must be unique")
        primary_key = None
        for column in stmt.columns:
            if column.primary_key:
                if primary_key is not None:
                    raise DBError("INVALID_CONSTRAINT", "Only one PRIMARY KEY column is supported")
                primary_key = column.name
        model_columns = []
        for column in stmt.columns:
            nullable = not (column.not_null or column.name == primary_key)
            model_columns.append(Column(column.name, column.type, nullable))

        constraints = []
        for constraint in stmt.constraints:
            if constraint.kind == "PRIMARY_KEY":
                if len(constraint.columns) != 1:
                    raise DBError("INVALID_CONSTRAINT", "Composite primary keys are not supported in v1")
                if primary_key is not None and primary_key != constraint.columns[0]:
                    raise DBError("INVALID_CONSTRAINT", "PRIMARY KEY declared twice")
                primary_key = constraint.columns[0]
            elif constraint.kind == "UNIQUE":
                self._validate_local_columns(constraint.columns, names)
                constraints.append(Constraint("UNIQUE", list(constraint.columns)))
            elif constraint.kind == "CHECK":
                check = self._bind_check(constraint.check, stmt.columns)
                constraints.append(Constraint("CHECK", expression=check))
            elif constraint.kind == "FOREIGN_KEY":
                constraints.append(self._build_foreign_key(constraint, stmt.name, names, primary_key))
        if primary_key is not None and primary_key not in names:
            raise DBError("INVALID_CONSTRAINT", "PRIMARY KEY must name a declared column")
        schema = TableSchema(stmt.name, tuple(model_columns), primary_key)
        self._need("TABLE", stmt.name, "CREATE")
        return schema, constraints

    def _validate_local_columns(self, columns, names):
        for column in columns:
            if column not in names:
                raise DBError("INVALID_CONSTRAINT", f"Unknown column {column!r} in constraint")

    def _bind_check(self, node, columns):
        scope = Scope([BoundColumn(None, c.name, c.name, c.type, not (c.not_null or c.primary_key))
                       for c in columns])
        bound = self._bind(node, scope)
        if contains_aggregate(node) or _is_subquery(node):
            raise DBError("INVALID_CONSTRAINT", "CHECK must be a row-local scalar expression")
        if bound.type not in {"BOOL", "NULL"}:
            raise DBError("TYPE_ERROR", "CHECK must be a boolean expression")
        return bound.expr

    def _build_foreign_key(self, constraint, table_name, names, primary_key):
        self._validate_local_columns(constraint.columns, names)
        if len(constraint.columns) != 1:
            raise DBError("INVALID_CONSTRAINT", "Only single-column foreign keys are supported in v1")
        ref_columns = constraint.ref_columns
        if ref_columns is None:
            if constraint.ref_table == table_name:
                ref_pk = primary_key
            else:
                ref = self._require_table(constraint.ref_table)
                ref_pk = ref.primary_key
            if ref_pk is None:
                raise DBError("INVALID_CONSTRAINT", "Referenced table has no primary key")
            ref_columns = [ref_pk]
        return Constraint("FOREIGN_KEY", list(constraint.columns), constraint.ref_table, list(ref_columns))

    def plan_create_index(self, stmt: ast.CreateIndexStmt) -> IndexSpec:
        info = self._require_table(stmt.table)
        if info.column(stmt.column) is None:
            raise DBError("UNKNOWN_COLUMN", f"Unknown column {stmt.column!r} in CREATE INDEX")
        self._need("INDEX", stmt.name, "CREATE")
        return IndexSpec(stmt.name, info.name, [stmt.column], stmt.unique)


# --- shared AST helpers --------------------------------------------------

def _is_subquery(node) -> bool:
    return isinstance(node, (ast.ScalarSubquery, ast.InSubquery, ast.ExistsExpr))


def contains_aggregate(node) -> bool:
    if isinstance(node, ast.FuncCall):
        if node.name.lower() in AGG_NAMES:
            return True
        return any(contains_aggregate(a) for a in node.args)
    if isinstance(node, ast.BinaryOp):
        return contains_aggregate(node.left) or contains_aggregate(node.right)
    if isinstance(node, ast.UnaryOp):
        return contains_aggregate(node.operand)
    if isinstance(node, ast.IsNull):
        return contains_aggregate(node.operand)
    if isinstance(node, ast.Between):
        return any(contains_aggregate(n) for n in (node.operand, node.low, node.high))
    if isinstance(node, ast.InList):
        return contains_aggregate(node.operand) or any(contains_aggregate(i) for i in node.items)
    # Subqueries are opaque: their aggregates belong to the inner query.
    return False


def _column_sources(node,slot):
    """Follow field producers for teaching; never read rows or change the plan."""
    if node.op == "SeqScan":
        column=next((c for c in node.output_columns if c.slot_id==slot),None)
        return [node.properties["table_id"]+"."+column.name] if column else []
    if node.op in {"Projection","Aggregate"}:
        position=next((i for i,c in enumerate(node.output_columns) if c.slot_id==slot),None)
        if position is not None:
            refs=[]
            def collect(expr):
                if expr.kind=="ColumnRef" and expr.level==0: refs.append(expr.slot_id)
                for arg in expr.args: collect(arg)
            collect(node.expressions[position])
            return sorted({source for ref in refs for child in node.children for source in _column_sources(child,ref)})
    return sorted({source for child in node.children for source in _column_sources(child,slot)})
