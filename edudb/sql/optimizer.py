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
    def __init__(self, catalog: CatalogView) -> None:
        self.catalog = catalog
        self._counter = 0

    def _nid(self, label: str) -> str:
        self._counter += 1
        return f"opt_{label}{self._counter}"

    def run(self, node: PlanNode) -> PlanNode:
        node.children = [self.run(child) for child in node.children]
        if node.op == "Filter":
            return self._pushdown(node)
        if node.op == "NestedLoopJoin":
            return self._hash_join(node)
        return node

    # --- predicate pushdown --------------------------------------------
    def _pushdown(self, filter_node: PlanNode) -> PlanNode:
        child = filter_node.children[0]
        conjuncts = split_and(filter_node.expressions[0])
        if child.op in {"NestedLoopJoin", "HashJoin"}:
            return self._push_into_join(filter_node, child, conjuncts)
        if child.op == "SeqScan":
            return self._index_scan(filter_node)
        return filter_node

    def _push_into_join(self, filter_node, join, conjuncts):
        left, right = join.children
        left_slots, right_slots = _slots(left), _slots(right)
        join_type = join.properties.get("join_type", "INNER")
        keep, to_left, to_right = [], [], []
        for conjunct in conjuncts:
            if _has_subquery(conjunct):
                keep.append(conjunct)
                continue
            refs = _refs(conjunct)
            if refs <= left_slots and join_type in {"INNER", "LEFT"}:
                to_left.append(conjunct)
            elif refs <= right_slots and join_type in {"INNER", "RIGHT"}:
                to_right.append(conjunct)
            else:
                keep.append(conjunct)
        if to_left:
            join.children[0] = self._attach(left, to_left)
        if to_right:
            join.children[1] = self._attach(right, to_right)
        join = self._hash_join(join) if join.op == "NestedLoopJoin" else join
        if not keep:
            return join
        filter_node.children = [join]
        filter_node.expressions = [combine_and(keep)]
        return filter_node

    def _attach(self, node: PlanNode, conjuncts: list[Expr]) -> PlanNode:
        wrapper = PlanNode(self._nid("filter"), "Filter", [node], list(node.output_columns),
                           [combine_and(conjuncts)])
        if node.op in {"NestedLoopJoin", "HashJoin"}:
            return self._push_into_join(wrapper, node, conjuncts)
        if node.op == "SeqScan":
            return self._index_scan(wrapper)
        return wrapper

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
                lower, lower_inc, has_bound = value, op == ">=", True
                used.add(i)
            elif op in {"<", "<="}:
                upper, upper_inc, has_bound = value, op == "<=", True
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
