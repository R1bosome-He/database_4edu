"""Rule-based optimizer: the optimizer-on counterpart of the baseline plan.

Rules (each preserves query semantics; the executor defines the semantics):
  * predicate pushdown - move WHERE conjuncts toward the scans, respecting the
    preserved side of outer joins;
  * index scan      - turn Filter(SeqScan) with a sargable predicate on an
    indexed column into an IndexScan plus a residual Filter;
  * hash join       - turn a NestedLoopJoin with an equi-join conjunct into a
    HashJoin, keeping the full ON condition as a residual check.

optimizer-off vs optimizer-on therefore differ only in physical shape, which is
what module C expects B to decide.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from ..c_api import Expr, PlanNode, RangeSpec
from .catalog import CatalogView

_COMPARISONS = {"=", "<>", "!=", "<", "<=", ">", ">="}
_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "="}


def optimize(plan: PlanNode, catalog: CatalogView) -> PlanNode:
    return Optimizer(catalog).run(plan)


def split_and(expr: Expr) -> list[Expr]:
    if expr.kind == "Binary" and expr.operator == "AND":
        return split_and(expr.args[0]) + split_and(expr.args[1])
    return [expr]


def combine_and(conjuncts: list[Expr]) -> Expr:
    result = conjuncts[0]
    for conjunct in conjuncts[1:]:
        result = Expr("Binary", operator="AND", args=[result, conjunct])
    return result


def _slots(node: PlanNode) -> set[str]:
    return {c.slot_id for c in node.output_columns}


def _refs(expr: Expr) -> set[str]:
    found = set()
    if expr.kind == "ColumnRef" and expr.level == 0:
        found.add(expr.slot_id)
    for arg in expr.args:
        found |= _refs(arg)
    return found


def _has_subquery(expr: Expr) -> bool:
    if expr.plan is not None:
        return True
    return any(_has_subquery(a) for a in expr.args)


class Optimizer:
    def __init__(self, catalog: CatalogView, trace=False) -> None:
        self.catalog = catalog
        self._counter = 0
        self.trace = trace
        self.steps = []
        self.step_count = 0

    def _nid(self, label: str) -> str:
        self._counter += 1
        return f"opt_{label}{self._counter}"

    def run(self, node: PlanNode) -> PlanNode:
        # Apply one real rewrite at a time. Each teaching snapshot therefore
        # describes the actual complete tree immediately before/after a rule.
        while True:
            before = asdict(node) if self.trace and len(self.steps) < 32 else None
            node, event = self._pass(node)
            if event is None:
                return node
            self.step_count += 1
            if before is not None:
                rule, reason, affected = event
                self.steps.append(dict(number=self.step_count, rule=rule,
                                       reason=reason, affected_nodes=affected,
                                       before=before, after=asdict(node)))

    def _pass(self, node):
        for i, child in enumerate(node.children):
            replacement, event = self._pass(child)
            if event is not None:
                node.children[i] = replacement
                return node, event
        if node.op == "Filter":
            child = node.children[0]
            if child.op == "Projection":
                mapping = {c.slot_id:e for c,e in zip(child.output_columns,child.expressions)}
                # Only rename/choose direct columns. Computed columns can throw,
                # so evaluating them on a different set of rows is not safe.
                direct = len(mapping) == len(child.output_columns) and all(
                    e.kind == "ColumnRef" and e.level == 0 for e in child.expressions)
                if direct and _safe_predicate(node.expressions[0]) and _refs(node.expressions[0]) <= mapping.keys():
                    predicate = _remap(node.expressions[0],mapping)
                    moved = PlanNode(self._nid("filter"),"Filter",[child.children[0]],
                                     list(child.children[0].output_columns),[predicate],
                                     applied_rules=["projection_pushdown"])
                    child.children = [moved]
                    child.applied_rules = list(dict.fromkeys(child.applied_rules+["projection_pushdown"]))
                    return child, ("projection_pushdown","投影只选择或重命名字段；按字段对应关系把外层筛选移到投影之前，不越过分组、去重或行数限制。",[node.node_id,child.node_id,moved.node_id])
            if child.op == "Filter" and all(_safe_predicate(e) for e in node.expressions+child.expressions):
                merged = PlanNode(self._nid("filter"),"Filter",child.children,list(node.output_columns),
                                  [combine_and(split_and(child.expressions[0])+split_and(node.expressions[0]))],
                                  applied_rules=["filter_merge"])
                return merged,("filter_merge","相邻筛选均为无子查询的简单条件，将条件用 AND 合并；索引条件之外仍保留剩余筛选。",[node.node_id,child.node_id,merged.node_id])
            if child.op in {"NestedLoopJoin","HashJoin"}:
                keep,left,right=[],[],[]
                left_slots,right_slots=map(_slots,child.children)
                kind=child.properties.get("join_type","INNER")
                for condition in split_and(node.expressions[0]):
                    refs=_refs(condition)
                    if not refs or not _safe_predicate(condition): keep.append(condition)
                    elif refs <= left_slots and kind in {"INNER","LEFT"}: left.append(condition)
                    elif refs <= right_slots and kind in {"INNER","RIGHT"}: right.append(condition)
                    else: keep.append(condition)
                if left or right:
                    for i, conditions in enumerate((left,right)):
                        if conditions:
                            branch=child.children[i]
                            child.children[i]=PlanNode(self._nid("filter"),"Filter",[branch],list(branch.output_columns),[combine_and(conditions)],applied_rules=["predicate_pushdown"])
                    child.applied_rules=list(dict.fromkeys(child.applied_rules+["predicate_pushdown"]))
                    if keep:
                        node.expressions=[combine_and(keep)]
                        result=node
                    else: result=child
                    return result,("predicate_pushdown","条件只引用一个连接输入，且满足外连接保留侧限制；先筛选该分支以减少参与连接的记录。",[node.node_id,child.node_id])
            if child.op == "SeqScan":
                replacement=self._index_scan(node)
                if replacement is not node:
                    return replacement,("index_scan","筛选列存在可用索引，比较条件可转成索引范围；使用索引定位并保留剩余条件。小表不保证更快。",[node.node_id,child.node_id,replacement.node_id])
        if node.op == "NestedLoopJoin":
            replacement=self._hash_join(node)
            if replacement is not node:
                return replacement,("hash_join","连接条件包含两侧字段的等值比较；建立哈希表并探测，同时保留完整连接条件及外连接语义。",[node.node_id,replacement.node_id])
        return node,None

    # --- index selection -----------------------------------------------
    def _index_scan(self, filter_node: PlanNode) -> PlanNode:
        scan = filter_node.children[0]
        if scan.op != "SeqScan":
            return filter_node
        info = self.catalog.get_table(scan.properties["table_id"])
        if info is None or not info.indexes:
            return filter_node
        slot_to_column = {c.slot_id: c.name for c in scan.output_columns}
        conjuncts = split_and(filter_node.expressions[0])
        for index in info.indexes:
            slot = next((s for s, name in slot_to_column.items() if name == index.column), None)
            if slot is None:
                continue
            bounds = self._range_for(conjuncts, slot)
            if bounds is None:
                continue
            spec, used = bounds
            indexed = PlanNode(self._nid("index"), "IndexScan", output_columns=list(scan.output_columns),
                               properties={"table_id": scan.properties["table_id"], "index_id": index.id,
                                           "range": spec, "columns": list(scan.properties.get("columns", []))},
                               applied_rules=["index_scan"])
            residual = [c for i, c in enumerate(conjuncts) if i not in used]
            if not residual:
                return indexed
            return PlanNode(self._nid("filter"), "Filter", [indexed], list(indexed.output_columns),
                            [combine_and(residual)], applied_rules=["index_scan"])
        return filter_node

    def _range_for(self, conjuncts, slot):
        lower = upper = None
        lower_inc = upper_inc = True
        used, has_bound = set(), False
        for i, conjunct in enumerate(conjuncts):
            sarg = self._sargable(conjunct, slot)
            if sarg is None:
                continue
            op, value = sarg
            if op == "=":
                return RangeSpec(value, value, True, True), {i}
            if op in {">", ">="}:
                if lower is None or value > lower:
                    lower, lower_inc = value, op == ">="
                elif value == lower:
                    lower_inc = lower_inc and op == ">="
                has_bound = True
                used.add(i)
            elif op in {"<", "<="}:
                if upper is None or value < upper:
                    upper, upper_inc = value, op == "<="
                elif value == upper:
                    upper_inc = upper_inc and op == "<="
                has_bound = True
                used.add(i)
        if not has_bound:
            return None
        return RangeSpec(lower, upper, lower_inc, upper_inc), used

    def _sargable(self, conjunct: Expr, slot: str):
        if conjunct.kind != "Binary" or conjunct.operator not in _COMPARISONS:
            return None
        if conjunct.operator in {"<>", "!="}:
            return None
        left, right = conjunct.args
        if left.kind == "ColumnRef" and left.level == 0 and left.slot_id == slot and _is_literal(right):
            return conjunct.operator, right.value
        if right.kind == "ColumnRef" and right.level == 0 and right.slot_id == slot and _is_literal(left):
            return _FLIP[conjunct.operator], left.value
        return None

    # --- hash join ------------------------------------------------------
    def _hash_join(self, join: PlanNode) -> PlanNode:
        if join.op != "NestedLoopJoin" or not join.expressions:
            return join
        left, right = join.children
        left_slots, right_slots = _slots(left), _slots(right)
        for conjunct in split_and(join.expressions[0]):
            if conjunct.kind != "Binary" or conjunct.operator != "=":
                continue
            a, b = conjunct.args
            if not (a.kind == "ColumnRef" and b.kind == "ColumnRef" and a.level == 0 and b.level == 0):
                continue
            if a.slot_id in left_slots and b.slot_id in right_slots:
                left_key, right_key = a, b
            elif a.slot_id in right_slots and b.slot_id in left_slots:
                left_key, right_key = b, a
            else:
                continue
            return PlanNode(self._nid("hashjoin"), "HashJoin", join.children, list(join.output_columns),
                            list(join.expressions),
                            {**join.properties, "left_key": left_key, "right_key": right_key},
                            applied_rules=["hash_join"])
        return join


def _is_literal(expr: Expr) -> bool:
    return expr.kind == "Literal" and expr.value is not None


def _safe_predicate(expr):
    if expr.kind == "Literal": return True
    if expr.kind == "ColumnRef": return expr.level == 0
    return (expr.kind == "Binary" and expr.operator in _COMPARISONS | {"AND","OR"}
            and all(_safe_predicate(arg) for arg in expr.args))


def _remap(expr,mapping):
    if expr.kind == "ColumnRef": return deepcopy(mapping[expr.slot_id])
    result=deepcopy(expr)
    result.args=[_remap(arg,mapping) for arg in expr.args]
    return result
