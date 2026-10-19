from __future__ import annotations

import copy
import functools
import sys
import time
from contextlib import contextmanager
from dataclasses import replace

from ..c_api import (DBError, Expr, IndexSpec, OperatorMetrics, QueryResult, RangeSpec)
from .expressions import checked_number, compare, evaluate, truth


def footprint(value) -> int:
    if isinstance(value, dict):
        return sys.getsizeof(value) + sum(footprint(k) + footprint(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return sys.getsizeof(value) + sum(footprint(v) for v in value)
    return sys.getsizeof(value)


class Operator:
    def __init__(self, executor, node, ctx) -> None:
        self.executor, self.node, self.ctx = executor, node, ctx
        self.iterator = None
        self.closed = False
        self._entered = False

    def open(self) -> None:
        if self.closed:
            raise DBError("OPERATOR_CLOSED", "Closed operators cannot be reopened")
        if self.iterator is None:
            self.executor.transactions.enter_operation(self.ctx.tx_id, self.ctx.session_id)
            self._entered = True
            self.iterator = self.executor._run(self.node, self.ctx)

    def next_batch(self, max_rows: int = 128):
        if max_rows < 1:
            raise DBError("INVALID_BATCH", "Batch size must be positive")
        if self.closed:
            return []
        self.open()
        result, size = [], 0
        try:
            for _ in range(max_rows):
                try:
                    row = next(self.iterator)
                except StopIteration:
                    self.close()
                    break
                size += footprint(row)
                if size + self.ctx.memory_used_bytes > self.ctx.memory_limit_bytes:
                    raise DBError("RESOURCE_LIMIT", "Batch exceeds memory limit")
                result.append(row)
            return result
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        try:
            if self.iterator is not None:
                self.iterator.close()
        finally:
            if self._entered:
                self.executor.transactions.leave_operation(self.ctx.tx_id)
                self._entered = False
            self.closed = True


class QueryExecutor:
    def __init__(self, records, indexes, transactions) -> None:
        self.records, self.indexes, self.transactions = records, indexes, transactions

    def open_operator(self, node, ctx):
        self.transactions.get_transaction(ctx.tx_id, ctx.session_id)
        self._validate(node)
        self.transactions.acquire_lock(ctx.tx_id, "catalog:schema", "S", ctx.lock_timeout_ms)
        for table in sorted(self._tables(node)):
            self._lock(table, ctx)
        return Operator(self, node, ctx)

    def evaluate(self, expr, row, ctx):
        return evaluate(expr, row, ctx, self._subquery)

    def _check(self, ctx):
        if ctx.cancelled:
            raise DBError("CANCELLED", "Query cancelled")
        self.transactions.get_transaction(ctx.tx_id, ctx.session_id)

    def _lock(self, table, ctx, mode="S"):
        self.transactions.acquire_lock(ctx.tx_id, "table:" + table, mode, ctx.lock_timeout_ms)

    @contextmanager
    def _budget(self, ctx):
        total = 0
        def retain(value):
            nonlocal total
            size = footprint(value)
            if ctx.memory_used_bytes + size > ctx.memory_limit_bytes:
                raise DBError("RESOURCE_LIMIT", "Materialization exceeds query memory budget")
            ctx.memory_used_bytes += size
            total += size
            return value
        try:
            yield retain
        finally:
            ctx.memory_used_bytes -= total

    def _map(self, columns, row):
        if len(columns) != len(row) or len({c.slot_id for c in columns}) != len(columns):
            raise DBError("INVALID_PLAN", "Output width or slot IDs invalid")
        return {c.slot_id: v for c, v in zip(columns, row)}

    def execute(self, plan, ctx):
        started = time.perf_counter()
        rows, affected = [], 0
        self.transactions.get_transaction(ctx.tx_id, ctx.session_id)
        self.transactions.enter_operation(ctx.tx_id, ctx.session_id)
        try:
            self._validate(plan)
            self.transactions.acquire_lock(ctx.tx_id, "catalog:schema", "S", ctx.lock_timeout_ms)
            # All tables (including nested subqueries) locked in stable order.
            tables = self._tables(plan)
            if plan.op in {"Insert", "Update", "Delete"} and hasattr(self.records, "related_tables"):
                tables.update(self.records.related_tables(plan.properties["table_id"]))
            for table in sorted(tables):
                self._lock(table, ctx, "X" if plan.op in {"Insert", "Update", "Delete"} and table == plan.properties.get("table_id") else "S")
            if plan.op in {"Insert", "Update", "Delete"}:
                affected = self._modify(plan, ctx)
            else:
                iterator = self._run(plan, ctx)
                with self._budget(ctx) as retain:
                    try:
                        for row in iterator:
                            rows.append(retain(row))
                    finally:
                        iterator.close()
            ctx.metrics.rows_returned = len(rows)
            return QueryResult(plan.output_columns, rows, affected, plan, ctx.metrics)
        except Exception as exc:
            # Storage/WAL errors poison the manager and require restart recovery;
            # do not claim a successful rollback if durable state is uncertain.
            if not self.transactions.needs_recovery:
                try:
                    self.transactions.get_transaction(ctx.tx_id, ctx.session_id)
                except DBError:
                    pass
                else:
                    self.transactions.rollback(ctx.tx_id)
            error = exc if isinstance(exc, DBError) else DBError("EXECUTION_ERROR", str(exc))
            error.transaction_effect = "TRANSACTION_ABORTED" if not self.transactions.needs_recovery else "NONE"
            raise error from exc
        finally:
            ctx.metrics.execution_ms = (time.perf_counter() - started) * 1000
            self.transactions.leave_operation(ctx.tx_id)

    def create_index(self, spec: IndexSpec, ctx):
        self.transactions.enter_operation(ctx.tx_id, ctx.session_id)
        try:
            self.transactions.acquire_lock(ctx.tx_id, "catalog:schema", "S", ctx.lock_timeout_ms)
            return self.indexes.create_index(spec, ctx)
        except Exception as exc:
            if not self.transactions.needs_recovery:
                self.transactions.rollback(ctx.tx_id)
            error = exc if isinstance(exc, DBError) else DBError("INDEX_ERROR", str(exc))
            error.transaction_effect = "TRANSACTION_ABORTED" if not self.transactions.needs_recovery else "NONE"
            raise error from exc
        finally:
            self.transactions.leave_operation(ctx.tx_id)

    def search_index(self, index_id, range_spec, ctx):
        return self.indexes.search_index(index_id, range_spec, ctx)

    def update_indexes(self, table_id, rid, before_values, after_values, ctx):
        return self.indexes.update_indexes(table_id, rid, before_values, after_values, ctx)

    def _validate(self, node):
        arity = {"Values": 0, "SeqScan": 0, "IndexScan": 0, "Filter": 1, "Projection": 1,
                 "Sort": 1, "Distinct": 1, "Limit": 1, "Aggregate": 1,
                 "NestedLoopJoin": 2, "HashJoin": 2, "Insert": 0, "Update": 0, "Delete": 0}
        if node.op not in arity or len(node.children) != arity[node.op]:
            raise DBError("INVALID_PLAN", f"Unsupported operator/arity: {node.op}")
        if len({c.slot_id for c in node.output_columns}) != len(node.output_columns):
            raise DBError("INVALID_PLAN", "Duplicate output slot IDs")
        for child in node.children:
            self._validate(child)
        if node.op in {"NestedLoopJoin", "HashJoin"}:
            left, right = node.children
            self._map(left.output_columns + right.output_columns, [None] * (len(left.output_columns) + len(right.output_columns)))
            expected = left.output_columns + right.output_columns
            if [(c.slot_id, c.type) for c in node.output_columns] != [(c.slot_id, c.type) for c in expected]:
                raise DBError("INVALID_PLAN", "Join output must concatenate child columns")
        if node.op == "Projection" and len(node.expressions) != len(node.output_columns):
            raise DBError("INVALID_PLAN", "Projection output width")

    def _tables(self, node):
        found = set()
        if "table_id" in node.properties:
            found.add(node.properties["table_id"])
        def exprs(value):
            if isinstance(value, Expr):
                if value.plan:
                    found.update(self._tables(value.plan))
                for arg in value.args:
                    exprs(arg)
            elif isinstance(value, (tuple, list)):
                for v in value:
                    exprs(v)
            elif isinstance(value, dict):
                for v in value.values():
                    exprs(v)
        exprs(node.expressions)
        exprs(node.properties)
        for child in node.children:
            found.update(self._tables(child))
        return found

    def _run(self, node, ctx):
        self._check(ctx)
        metric = ctx.metrics.per_operator.setdefault(node.node_id, OperatorMetrics(node.node_id))
        started = time.perf_counter()
        iterator = self._rows(node, ctx, metric)
        try:
            for row in iterator:
                self._check(ctx)
                self._map(node.output_columns, row)
                metric.output_rows += 1
                yield row
        finally:
            iterator.close()
            metric.elapsed_ms += (time.perf_counter() - started) * 1000

    def _child(self, child, ctx):
        iterator = self._run(child, ctx)
        try:
            yield from iterator
        finally:
            iterator.close()

    def _rows(self, node, ctx, metric):
        op, props = node.op, node.properties
        if op == "Values":
            for row in props.get("rows", []):
                yield list(row)
            return
        if op in {"SeqScan", "IndexScan"}:
            table = props["table_id"]
            self._lock(table, ctx)
            fields = props.get("columns", [c.name for c in self.records.schema(table).columns])
            if op == "SeqScan":
                records = self.records.scan(table, ctx)
            else:
                spec = props.get("range", RangeSpec())
                if isinstance(spec, dict):
                    spec = RangeSpec(**spec)
                records = (self.records.fetch(table, rid, ctx) for rid in self.indexes.search_index(props["index_id"], spec, ctx))
            for record in records:
                metric.rows_examined += 1
                yield [record.values[name] for name in fields]
            return
        if op in {"Filter", "Projection", "Limit", "Distinct"}:
            child = node.children[0]
            offset, limit = props.get("offset", 0), props.get("limit")
            if offset < 0 or (limit is not None and limit < 0):
                raise DBError("INVALID_PLAN", "Negative LIMIT/OFFSET")
            seen = set()
            emitted = 0
            if op == "Limit" and limit == 0:
                return
            with self._budget(ctx) as retain:
                for index, row in enumerate(self._child(child, ctx)):
                    metric.input_rows += 1
                    env = self._map(child.output_columns, row)
                    if op == "Filter":
                        if truth(self.evaluate(node.expressions[0], env, ctx)) is True:
                            yield row
                    elif op == "Projection":
                        yield [self.evaluate(expr, env, ctx) for expr in node.expressions]
                    elif op == "Distinct":
                        key = tuple(row)
                        if key not in seen:
                            seen.add(retain(key))
                            yield row
                    elif index >= offset:
                        yield row
                        emitted += 1
                        if limit is not None and emitted >= limit:
                            return
            return
        if op == "Sort":
            child = node.children[0]
            order = props.get("order_by", [])
            with self._budget(ctx) as retain:
                decorated = []
                for row in self._child(child, ctx):
                    metric.input_rows += 1
                    keys = [self.evaluate(item["expr"], self._map(child.output_columns, row), ctx) for item in order]
                    decorated.append(retain((row, keys)))
                def cmp(a, b):
                    for item, left, right in zip(order, a[1], b[1]):
                        if left is None or right is None:
                            if left is right:
                                continue
                            null_first = item.get("nulls_first", False)
                            return (-1 if null_first else 1) if left is None else (1 if null_first else -1)
                        result = (1 if compare(left, right, ">") else -1 if compare(left, right, "<") else 0)
                        if result:
                            return -result if item.get("descending", False) else result
                    return 0
                decorated.sort(key=functools.cmp_to_key(cmp))
                for row, _ in decorated:
                    yield row
            return
        if op in {"NestedLoopJoin", "HashJoin"}:
            yield from self._join(node, ctx, metric)
            return
        if op == "Aggregate":
            child = node.children[0]
            groups = {}
            keys = props.get("group_by", [])
            with self._budget(ctx) as retain:
                for row in self._child(child, ctx):
                    metric.input_rows += 1
                    env = self._map(child.output_columns, row)
                    key = tuple(self.evaluate(e, env, ctx) for e in keys)
                    if key not in groups:
                        retain(key)
                        retain([])
                    groups.setdefault(key, []).append(retain(env))
                if not groups and not keys:
                    groups[()] = []
                for group in groups.values():
                    yield [self._group_eval(e, group, ctx) for e in node.expressions]
            return
        raise DBError("INVALID_PLAN", f"Cannot stream operator {op}")

    def _join(self, node, ctx, metric):
        left, right = node.children
        kind = node.properties.get("join_type", "INNER")
        if kind not in {"INNER", "LEFT", "RIGHT", "FULL"}:
            raise DBError("INVALID_PLAN", "Unknown join type")
        condition = node.expressions[0] if node.expressions else Expr("Literal", True)
        columns = left.output_columns + right.output_columns
        with self._budget(ctx) as retain:
            right_rows = []
            for row in self._child(right, ctx):
                right_rows.append(retain(row))
                metric.input_rows += 1
            matched = retain([False] * len(right_rows))
            buckets = {}
            hash_join = node.op == "HashJoin"
            if hash_join:
                for index, row in enumerate(right_rows):
                    key = self.evaluate(node.properties["right_key"], self._map(right.output_columns, row), ctx)
                    metric.hash_build_rows += 1
                    if key is not None:
                        if key not in buckets:
                            retain(key)
                            retain([])
                        buckets.setdefault(key, []).append(retain(index))
            for lrow in self._child(left, ctx):
                metric.input_rows += 1
                found = False
                if hash_join:
                    key = self.evaluate(node.properties["left_key"], self._map(left.output_columns, lrow), ctx)
                    metric.hash_probes += 1
                    candidates = buckets.get(key, []) if key is not None else []
                else:
                    candidates = range(len(right_rows))
                for index in candidates:
                    self._check(ctx)
                    row = lrow + right_rows[index]
                    metric.join_comparisons += 1
                    if truth(self.evaluate(condition, self._map(columns, row), ctx)) is True:
                        found = matched[index] = True
                        yield row
                if not found and kind in {"LEFT", "FULL"}:
                    yield lrow + [None] * len(right.output_columns)
            if kind in {"RIGHT", "FULL"}:
                for index, row in enumerate(right_rows):
                    if not matched[index]:
                        yield [None] * len(left.output_columns) + row

    def _group_eval(self, expr, group, ctx):
        if expr.kind == "Aggregate":
            name = expr.operator
            if name not in {"COUNT", "SUM", "AVG", "MIN", "MAX"}:
                raise DBError("UNSUPPORTED_EXPRESSION", f"Aggregate {name}")
            if not expr.args:
                if name != "COUNT" or expr.distinct:
                    raise DBError("INVALID_PLAN", "Only COUNT(*) accepts no argument")
                return len(group)
            count, accumulator, seen = 0, None, set()
            with self._budget(ctx) as retain:
                for row in group:
                    self._check(ctx)
                    value = self.evaluate(expr.args[0], row, ctx)
                    if value is None or (expr.distinct and value in seen):
                        continue
                    if expr.distinct:
                        seen.add(retain(value))
                    count += 1
                    if name in {"SUM", "AVG"}:
                        if type(value) not in (int, float):
                            raise DBError("TYPE_ERROR", "SUM/AVG require numeric values")
                        accumulator = value if accumulator is None else accumulator + value
                    elif name in {"MIN", "MAX"}:
                        if accumulator is None or compare(value, accumulator, "<" if name == "MIN" else ">"):
                            accumulator = value
            if name == "COUNT":
                return count
            if not count:
                return None
            return checked_number(accumulator / count if name == "AVG" else accumulator)
        if expr.args:
            expr = replace(expr, args=[Expr("Literal", self._group_eval(a, group, ctx)) for a in expr.args])
        return self.evaluate(expr, group[0] if group else {}, ctx)

    def _subquery(self, expr, env, ctx):
        if expr.plan is None:
            raise DBError("INVALID_PLAN", "Subquery plan is missing")
        op = expr.operator or "SCALAR"
        if op not in {"SCALAR", "EXISTS", "NOT EXISTS", "IN", "NOT IN"}:
            raise DBError("INVALID_PLAN", "Unknown subquery mode")
        value = self.evaluate(expr.args[0], env, ctx) if op in {"IN", "NOT IN"} else None
        self._validate(expr.plan)
        if op not in {"EXISTS", "NOT EXISTS"} and len(expr.plan.output_columns) != 1:
            raise DBError("SUBQUERY_WIDTH", "Scalar/IN subquery must return one column")
        ctx.outer_rows.insert(0, env)
        iterator = self._run(expr.plan, ctx)
        try:
            if op in {"EXISTS", "NOT EXISTS"}:
                found = next(iterator, None) is not None
                return not found if op == "NOT EXISTS" else found
            if op == "SCALAR":
                first = next(iterator, None)
                if first is None:
                    return None
                if next(iterator, None) is not None:
                    raise DBError("SUBQUERY_CARDINALITY", "Scalar subquery returned multiple rows")
                return first[0]
            unknown = False
            for row in iterator:
                result = compare(value, row[0], "=")
                if result is True:
                    return op == "IN"
                unknown |= result is None
            result = None if unknown else False
            return (None if result is None else not result) if op == "NOT IN" else result
        finally:
            iterator.close()
            ctx.outer_rows.pop(0)

    def _modify(self, plan, ctx):
        table = plan.properties["table_id"]
        self._lock(table, ctx, "X")
        schema = self.records.schema(table)
        metric = ctx.metrics.per_operator.setdefault(plan.node_id, OperatorMetrics(plan.node_id))
        affected = 0
        if plan.op == "Insert":
            for values in plan.properties.get("rows", []):
                self._check(ctx)
                converted = {k: self.evaluate(v, {}, ctx) if isinstance(v, Expr) else v for k, v in values.items()}
                # Prevalidate before any mutation; A checks types and primary key too.
                schema.validate_record(converted)
                self._constraints(table, schema.validate_record(converted), None, ctx)
                rid = self.records.insert(table, converted, ctx)
                actual = self.records.fetch(table, rid, ctx).values
                self.indexes.update_indexes(table, rid, None, actual, ctx)
                affected += 1
        else:
            columns = plan.properties.get("input_columns")
            if columns is None:
                raise DBError("INVALID_PLAN", "DML requires bound input_columns")
            fields = plan.properties.get("columns", [c.name for c in schema.columns])
            with self._budget(ctx) as retain:
                selected = []
                # Snapshot target rows before modifications: no Halloween revisits.
                for record in self.records.scan(table, ctx):
                    self._check(ctx)
                    metric.rows_examined += 1
                    env = self._map(columns, [record.values[n] for n in fields])
                    if not plan.expressions or truth(self.evaluate(plan.expressions[0], env, ctx)) is True:
                        selected.append(retain((record.rid, record.values, env)))
                for rid, before, env in selected:
                    self._check(ctx)
                    if plan.op == "Delete":
                        self._check_references(table, before, None, ctx)
                        self.records.delete(table, rid, ctx)
                        self.indexes.update_indexes(table, rid, before, None, ctx)
                    else:
                        assignments = {name: self.evaluate(expr, env, ctx) for name, expr in plan.properties["assignments"].items()}
                        proposed = schema.validate_record({**before, **assignments})
                        self._constraints(table, proposed, rid, ctx)
                        self._check_references(table, before, proposed, ctx)
                        self.records.update(table, rid, assignments, ctx)
                        after = self.records.fetch(table, rid, ctx).values
                        self.indexes.update_indexes(table, rid, before, after, ctx)
                    affected += 1
        metric.output_rows = affected
        return affected

    def _constraints(self, table, values, rid, ctx):
        if not hasattr(self.records, "constraints"):
            return
        for rule in self.records.constraints(table):
            key = tuple(values[c] for c in rule.columns)
            if rule.kind == "CHECK":
                # CHECK accepts TRUE/UNKNOWN, unlike WHERE. Rule slots are column IDs
                # (column names in the current A compatibility bridge).
                if truth(self.evaluate(rule.expression, values, ctx)) is False:
                    raise DBError("CHECK_VIOLATION", "CHECK constraint failed")
            elif rule.kind == "UNIQUE" and all(v is not None for v in key):
                for old in self.records.scan(table, ctx):
                    if old.rid != rid and tuple(old.values[c] for c in rule.columns) == key:
                        raise DBError("UNIQUE_VIOLATION", "UNIQUE constraint failed")
            elif rule.kind == "FOREIGN_KEY" and all(v is not None for v in key):
                self_reference = rule.ref_table == table and tuple(values[c] for c in rule.ref_columns) == key
                if not self_reference and not any(tuple(r.values[c] for c in rule.ref_columns) == key
                                                  for r in self.records.scan(rule.ref_table, ctx)):
                    raise DBError("FOREIGN_KEY_VIOLATION", "Referenced key does not exist")

    def _check_references(self, table, before, after, ctx):
        if not hasattr(self.records, "constraints"):
            return
        for dependent in self.records.catalog.tables():
            for rule in self.records.constraints(dependent.name):
                if rule.kind != "FOREIGN_KEY" or rule.ref_table != table:
                    continue
                old_key = tuple(before[c] for c in rule.ref_columns)
                if after is not None and tuple(after[c] for c in rule.ref_columns) == old_key:
                    continue
                for row in self.records.scan(dependent.name, ctx):
                    key = tuple(row.values[c] for c in rule.columns)
                    if all(v is not None for v in key) and key == old_key:
                        # RESTRICT is immediate; self-referential parent deletion
                        # is conservatively rejected too in this first version.
                        raise DBError("FOREIGN_KEY_VIOLATION", "Referenced row cannot be deleted/changed")
