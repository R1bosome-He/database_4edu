"""Bound expression evaluation: SQL three-valued logic, not Python truthiness."""
from __future__ import annotations

import math
import operator

from ..c_api import DBError, Expr


def checked_number(value):
    if type(value) is int and not -(2**63) <= value < 2**63:
        raise DBError("NUMERIC_OVERFLOW", "INT result exceeds signed 64-bit range")
    if type(value) is float and not math.isfinite(value):
        raise DBError("NUMERIC_OVERFLOW", "Non-finite FLOAT result")
    return value


def truth(value):
    if value is not None and type(value) is not bool:
        raise DBError("TYPE_ERROR", "Boolean expression expected")
    return value


def compare(left, right, operation):
    if left is None or right is None:
        return None
    numeric = type(left) in (int, float) and type(right) in (int, float)
    if not numeric and type(left) is not type(right):
        raise DBError("TYPE_ERROR", "Incompatible comparison types")
    operations = {"=": operator.eq, "!=": operator.ne, "<>": operator.ne,
                  "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}
    if operation not in operations:
        raise DBError("UNSUPPORTED_EXPRESSION", operation)
    return operations[operation](left, right)


def evaluate(expr: Expr, row: dict, ctx, subquery) -> object:
    if expr.kind == "Literal":
        value = expr.value
        if value is not None and type(value) not in (int, float, str, bool):
            raise DBError("TYPE_ERROR", "Literal must be a SQL scalar")
        return checked_number(value)
    if expr.kind == "ColumnRef":
        if expr.level < 0 or expr.level > len(ctx.outer_rows):
            raise DBError("INVALID_COLUMN", "Invalid correlation level")
        scope = row if expr.level == 0 else ctx.outer_rows[expr.level - 1]
        if expr.slot_id not in scope:
            raise DBError("INVALID_COLUMN", f"Unknown bound slot: {expr.slot_id}")
        return scope[expr.slot_id]
    if expr.kind == "Subquery":
        return subquery(expr, row, ctx)
    if expr.kind == "IsNull":
        value = evaluate(expr.args[0], row, ctx, subquery) is None
        return not value if expr.operator == "IS NOT NULL" else value
    if expr.kind == "Unary":
        value = evaluate(expr.args[0], row, ctx, subquery)
        if expr.operator == "NOT":
            return None if truth(value) is None else not value
        if expr.operator in {"+", "-"}:
            if value is None:
                return None
            if type(value) not in (int, float):
                raise DBError("TYPE_ERROR", "Numeric expression expected")
            return checked_number(value if expr.operator == "+" else -value)
    if expr.kind == "Binary":
        op = expr.operator
        left = evaluate(expr.args[0], row, ctx, subquery)
        if op == "AND" and truth(left) is False:
            return False
        if op == "OR" and truth(left) is True:
            return True
        right = evaluate(expr.args[1], row, ctx, subquery)
        if op in {"AND", "OR"}:
            truth(left), truth(right)
            if op == "AND":
                return False if right is False else (None if left is None or right is None else True)
            return True if right is True else (None if left is None or right is None else False)
        if op in {"=", "!=", "<>", "<", "<=", ">", ">="}:
            return compare(left, right, op)
        if op in {"+", "-", "*", "/", "%"}:
            if left is None or right is None:
                return None
            if type(left) not in (int, float) or type(right) not in (int, float):
                raise DBError("TYPE_ERROR", "Arithmetic requires numbers")
            if op in {"/", "%"} and right == 0:
                raise DBError("DIVISION_BY_ZERO", "Division by zero")
            value = {"+": operator.add, "-": operator.sub, "*": operator.mul,
                     "/": operator.truediv, "%": operator.mod}[op](left, right)
            return checked_number(value)
    raise DBError("UNSUPPORTED_EXPRESSION", f"Unsupported {expr.kind}/{expr.operator}")
