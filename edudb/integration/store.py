"""Reference page backend: 4KB checked envelopes, page LSN, write-through LRU.

This is deliberately separate from A's unchanged v1 DiskManager: its page format
has no page LSN/index payload. No implicit migration of v1 data takes place.
"""
from __future__ import annotations

import copy
import json
import os
import struct
import threading
import zlib
from collections import OrderedDict
from pathlib import Path

from ..c_api import DBError, Metrics, PageChange, PageKey
from ..fileio import atomic_write
from ..models import Page as LegacyPage, identifier
from ..storage.page import encode_page
from ..storage.records import RecordStore

PAGE_SIZE = 4096
HEADER = struct.Struct(">4sII")


class ReferencePageStore:
    def __init__(self, root: str | Path, capacity: int = 16) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.root = Path(root) / "c_pages_v1"
        self.root.mkdir(parents=True, exist_ok=True)
        self.capacity = capacity
        self._cache: OrderedDict[PageKey, dict] = OrderedDict()
        self._mutex = threading.RLock()
        self.ensure_log_durable = lambda lsn: (_ for _ in ()).throw(DBError("WAL_NOT_BOUND", "Bind WAL first"))

    def _path(self, key: PageKey) -> Path:
        if type(key.page_id) is not int or not 0 <= key.page_id < 100_000_000:
            raise DBError("INVALID_PAGE", "Invalid page number")
        return self.root / identifier(key.file_id) / f"{key.page_id:08d}.page"

    @staticmethod
    def encode(envelope: dict) -> bytes:
        raw = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(raw) > PAGE_SIZE - HEADER.size:
            raise DBError("PAGE_FULL", "Page image exceeds 4KB; overflow pages are not implemented")
        return HEADER.pack(b"EDC1", len(raw), zlib.crc32(raw)) + raw + bytes(PAGE_SIZE - HEADER.size - len(raw))

    def validate_image(self, image: dict | None) -> None:
        self.encode(dict(page_lsn=2**63 - 1, image=image))

    def _load(self, key: PageKey, metrics: Metrics | None) -> dict:
        if key in self._cache:
            self._cache.move_to_end(key)
            if metrics:
                metrics.cache_hits += 1
            return copy.deepcopy(self._cache[key])
        path = self._path(key)
        if metrics:
            metrics.cache_misses += 1
        if not path.exists():
            return dict(page_lsn=0, image=None)
        raw = path.read_bytes()
        if metrics:
            if key.file_id.startswith("ci_"):
                metrics.index_page_reads += 1
            else:
                metrics.data_page_reads += 1
        if len(raw) != PAGE_SIZE:
            raise DBError("PAGE_CORRUPTION", f"Invalid page size: {key}")
        magic, size, crc = HEADER.unpack_from(raw)
        payload = raw[HEADER.size:HEADER.size + size]
        if magic != b"EDC1" or size > PAGE_SIZE - HEADER.size or zlib.crc32(payload) != crc or any(raw[HEADER.size + size:]):
            raise DBError("PAGE_CORRUPTION", f"Bad page checksum/header: {key}")
        try:
            env = json.loads(payload)
            if type(env["page_lsn"]) is not int or env["page_lsn"] < 0 or "image" not in env:
                raise ValueError("Bad envelope")
        except (ValueError, KeyError, TypeError) as exc:
            raise DBError("PAGE_CORRUPTION", f"Bad page envelope: {key}") from exc
        self._remember(key, env, metrics)
        return copy.deepcopy(env)

    def _remember(self, key: PageKey, env: dict, metrics: Metrics | None = None) -> None:
        self._cache[key] = copy.deepcopy(env)
        self._cache.move_to_end(key)
        if len(self._cache) > self.capacity:
            self._cache.popitem(last=False)
            if metrics:
                metrics.evictions += 1

    def read_image(self, key: PageKey, metrics: Metrics | None = None) -> dict | None:
        with self._mutex:
            return self._load(key, metrics)["image"]

    def apply_image(self, key: PageKey, image: dict | None, lsn: int) -> None:
        with self._mutex:
            current = self._load(key, None)
            if current["page_lsn"] >= lsn:
                return
            self.ensure_log_durable(lsn)
            envelope = dict(page_lsn=lsn, image=image)
            raw = self.encode(envelope)
            atomic_write(self._path(key), raw)
            self._remember(key, envelope)

    def page_ids(self, file_id: str, metrics=None) -> tuple[int, ...]:
        with self._mutex:
            directory = self.root / identifier(file_id)
            keys = [PageKey(file_id, int(p.stem)) for p in directory.glob("*.page")]
            return tuple(sorted(k.page_id for k in keys if self.read_image(k, metrics) is not None))

    def flush_all(self) -> None:
        # Write-through: apply_image already fsyncs each page through atomic_write.
        pass

    def sync_files(self, file_ids: list[str] | None = None) -> None:
        directories = [self.root / identifier(f) for f in file_ids] if file_ids else list(self.root.iterdir())
        for directory in directories:
            if not directory.is_dir():
                continue
            for path in directory.glob("*.page"):
                # Windows _commit requires a write-capable file descriptor.
                with path.open("r+b") as stream:
                    os.fsync(stream.fileno())

    def reset_cache(self) -> None:
        with self._mutex:
            self._cache.clear()


class TransactionalPageIO:
    """Makes A's existing RecordStore writes use C's physical WAL."""
    def __init__(self, store, transactions, context) -> None:
        self.store, self.transactions, self.ctx = store, transactions, context

    def read_page(self, table: str, page_id: int) -> LegacyPage:
        image = self.store.read_image(PageKey("ct_" + identifier(table), page_id), self.ctx.metrics)
        if image is None:
            raise DBError("RECORD_NOT_FOUND", "Missing data page")
        return LegacyPage(page_id, copy.deepcopy(image["payload"]))

    def write_page(self, table: str, page: LegacyPage) -> None:
        encode_page(page)  # Keep A's record validation and 4KB record constraints.
        key = PageKey("ct_" + identifier(table), page.page_id)
        image = dict(kind="DATA", payload=copy.deepcopy(page.slots))
        self.store.validate_image(image)
        before = self.store.read_image(key, self.ctx.metrics)
        self.transactions.write_change(self.ctx.tx_id, PageChange(key, before, image))
        self.ctx.metrics.page_writes += 1

    def page_ids(self, table: str) -> tuple[int, ...]:
        return self.store.page_ids("ct_" + identifier(table), self.ctx.metrics)


class RecordAdapter:
    def __init__(self, catalog, pages, transactions) -> None:
        self.catalog, self.pages, self.transactions = catalog, pages, transactions

    def schema(self, table_id):
        return self.catalog.table(table_id)

    def constraints(self, table_id):
        return self.catalog.constraints(table_id)

    def related_tables(self, table_id):
        result = {table_id}
        for table in self.catalog.tables():
            for rule in self.constraints(table.name):
                if rule.kind == "FOREIGN_KEY" and (table.name == table_id or rule.ref_table == table_id):
                    result.update((table.name, rule.ref_table))
        return result

    def _records(self, ctx):
        self.transactions.get_transaction(ctx.tx_id, ctx.session_id)
        return RecordStore(self.catalog, TransactionalPageIO(self.pages, self.transactions, ctx))

    def scan(self, table_id, ctx):
        return self._records(ctx).scan(table_id)

    def fetch(self, table_id, rid, ctx):
        return self._records(ctx).fetch(table_id, rid)

    def insert(self, table_id, values, ctx):
        from ..models import RecordId
        schema = self.schema(table_id)
        record = schema.validate_record(values)
        if schema.primary_key:
            for old in self.scan(table_id, ctx):
                if old.values[schema.primary_key] == record[schema.primary_key]:
                    raise DBError("PRIMARY_KEY_VIOLATION", "Duplicate primary key")
        pages = TransactionalPageIO(self.pages, self.transactions, ctx)
        ids = pages.page_ids(table_id)
        page = pages.read_page(table_id, ids[-1]) if ids else LegacyPage(0, [])
        page.slots.append(record)
        try:
            self.pages.validate_image(dict(kind="DATA", payload=page.slots))
        except DBError as error:
            if error.code != "PAGE_FULL" or len(page.slots) == 1:
                raise
            page = LegacyPage(page.page_id + 1, [record])
        pages.write_page(table_id, page)
        return RecordId(page.page_id, len(page.slots) - 1)

    def update(self, table_id, rid, values, ctx):
        return self._records(ctx).update(table_id, rid, values)

    def delete(self, table_id, rid, ctx):
        return self._records(ctx).delete(table_id, rid)
