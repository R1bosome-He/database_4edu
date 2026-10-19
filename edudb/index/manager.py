from __future__ import annotations

import json
import uuid
from bisect import bisect_right
from dataclasses import asdict

from ..c_api import DBError, IndexInfo, IndexSpec, PageChange, PageKey, RangeSpec
from ..models import RecordId, identifier
from .bplus import BPlusTree

CATALOG = PageKey("ci_catalog", 0)


class IndexManager:
    def __init__(self, store, records, transactions, order: int = 8) -> None:
        self.store, self.records, self.transactions, self.order = store, records, transactions, order

    def _catalog(self, ctx):
        image = self.store.read_image(CATALOG, ctx.metrics)
        return image["payload"] if image else {}

    def _change(self, key, payload, ctx):
        before = self.store.read_image(key, ctx.metrics)
        after = None if payload is None else dict(kind="INDEX", payload=payload)
        self.store.validate_image(after)
        self.transactions.write_change(ctx.tx_id, PageChange(key, before, after))
        ctx.metrics.page_writes += 1

    def _lock(self, ctx, resource, mode):
        self.transactions.get_transaction(ctx.tx_id, ctx.session_id)
        self.transactions.acquire_lock(ctx.tx_id, resource, mode, ctx.lock_timeout_ms)

    def list_indexes(self, table_id, ctx):
        self._lock(ctx, "table:" + table_id, "S")
        self._lock(ctx, "catalog:indexes", "S")
        return [IndexInfo(**value) for value in self._catalog(ctx).values() if value["table_id"] == table_id]

    def _load(self, info, ctx):
        meta = self.store.read_image(PageKey(info.id, 0), ctx.metrics)
        if not meta:
            raise DBError("INDEX_CORRUPTION", "Index superblock is missing")
        config = meta["payload"]
        payloads = {}
        pending = [config["root"]]
        size = 0
        while pending:
            node_id = pending.pop()
            if node_id in payloads:
                raise DBError("INDEX_CORRUPTION", "Repeated child")
            image = self.store.read_image(PageKey(info.id, node_id), ctx.metrics)
            if image is None:
                raise DBError("INDEX_CORRUPTION", "Missing index node")
            payloads[node_id] = image["payload"]
            size += len(json.dumps(image).encode("utf-8")) * 4
            if size + ctx.memory_used_bytes > ctx.memory_limit_bytes:
                raise DBError("RESOURCE_LIMIT", "Index working set exceeds query memory budget")
            pending.extend(image["payload"]["children"])
        return BPlusTree.load(config["order"], config["root"], config["next_id"], payloads)

    def _save(self, info, tree, ctx):
        tree.validate()
        after = tree.dump()
        old = self.store.read_image(PageKey(info.id, 0), ctx.metrics)
        previous = set(old["payload"]["nodes"]) if old else set()
        # Validate every image BEFORE publishing any node. Transaction rollback
        # still protects against subsequent I/O failures.
        for payload in after.values():
            self.store.validate_image(dict(kind="INDEX", payload=payload))
        for node_id, payload in after.items():
            key = PageKey(info.id, node_id)
            current = self.store.read_image(key, ctx.metrics)
            if current is None or current["payload"] != payload:
                self._change(key, payload, ctx)
        for node_id in previous - set(after):
            self._change(PageKey(info.id, node_id), None, ctx)
        self._change(PageKey(info.id, 0), dict(root=tree.root, next_id=tree.next_id,
                                             order=tree.order, nodes=sorted(after)), ctx)

    def create_index(self, spec: IndexSpec, ctx) -> IndexInfo:
        self._lock(ctx, "table:" + spec.table_id, "X")
        self._lock(ctx, "catalog:indexes", "X")
        schema = self.records.schema(spec.table_id)
        if len(spec.column_ids) != 1 or spec.column_ids[0] not in [c.name for c in schema.columns]:
            raise DBError("INVALID_INDEX", "Exactly one existing column is required")
        identifier(spec.name)
        catalog = self._catalog(ctx)
        if any(v["name"] == spec.name for v in catalog.values()):
            raise DBError("INDEX_EXISTS", spec.name)
        tree = BPlusTree(self.order)
        column = spec.column_ids[0]
        keys = set()
        size = 0
        for record in self.records.scan(spec.table_id, ctx):
            value = record.values[column]
            if value is not None and spec.unique and value in keys:
                raise DBError("UNIQUE_VIOLATION", "Existing duplicate prevents unique index")
            if value is not None:
                keys.add(value)
                size += 192 + len(repr(value).encode("utf-8"))
                if size + ctx.memory_used_bytes > ctx.memory_limit_bytes:
                    raise DBError("RESOURCE_LIMIT", "Index construction exceeds memory budget")
            tree.insert(value, record.rid)
        info = IndexInfo("ci_" + uuid.uuid4().hex, spec.name, spec.table_id, list(spec.column_ids), spec.unique, tree.root)
        self._save(info, tree, ctx)
        catalog[info.id] = asdict(info)
        self._change(CATALOG, catalog, ctx)
        return info

    def search_index(self, index_id, range_spec: RangeSpec, ctx):
        # Caller/executor pre-locks table before catalog to use one lock order.
        self._lock(ctx, "catalog:indexes", "S")
        raw = self._catalog(ctx).get(index_id)
        if raw is None:
            raise DBError("INDEX_NOT_FOUND", index_id)
        info = IndexInfo(**raw)
        self._lock(ctx, "table:" + info.table_id, "S")
        meta = self.store.read_image(PageKey(info.id, 0), ctx.metrics)
        if meta is None:
            raise DBError("INDEX_CORRUPTION", "Missing superblock")
        visited = set()
        def read(node_id):
            if ctx.cancelled:
                raise DBError("CANCELLED", "Index query cancelled")
            if node_id in visited:
                raise DBError("INDEX_CORRUPTION", "Cyclic index path/leaf chain")
            visited.add(node_id)
            if len(visited) * 64 + ctx.memory_used_bytes + 16384 > ctx.memory_limit_bytes:
                raise DBError("RESOURCE_LIMIT", "Index traversal exceeds memory budget")
            image = self.store.read_image(PageKey(info.id, node_id), ctx.metrics)
            if image is None:
                raise DBError("INDEX_CORRUPTION", "Missing B+ node")
            return image["payload"]
        node = read(meta["payload"]["root"])
        lower = (range_spec.lower, -1, -1) if range_spec.lower is not None else None
        while not node["leaf"]:
            position = bisect_right([tuple(k) for k in node["keys"]], lower) if lower is not None else 0
            node = read(node["children"][position])
        while True:
            for value, page, slot in node["keys"]:
                if range_spec.lower is not None and (value < range_spec.lower or (value == range_spec.lower and not range_spec.lower_inclusive)):
                    continue
                if range_spec.upper is not None and (value > range_spec.upper or (value == range_spec.upper and not range_spec.upper_inclusive)):
                    return
                yield RecordId(page, slot)
            if node["next"] is None:
                return
            node = read(node["next"])

    def update_indexes(self, table_id, rid, before_values, after_values, ctx):
        self._lock(ctx, "table:" + table_id, "X")
        self._lock(ctx, "catalog:indexes", "X")
        for info in self.list_indexes(table_id, ctx):
            tree = self._load(info, ctx)
            column = info.column_ids[0]
            old = before_values.get(column) if before_values is not None else None
            new = after_values.get(column) if after_values is not None else None
            if before_values is not None:
                tree.delete(old, rid)
            if new is not None and info.unique and any(tree.search(RangeSpec(new, new))):
                raise DBError("UNIQUE_VIOLATION", f"Duplicate key in {info.name}")
            if after_values is not None:
                tree.insert(new, rid)
            self._save(info, tree, ctx)
            if tree.root != info.root_page_id:
                info.root_page_id = tree.root
                catalog = self._catalog(ctx)
                catalog[info.id] = asdict(info)
                self._change(CATALOG, catalog, ctx)
