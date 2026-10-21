"""B/C execution on A's real StorageService; explicit boundary conversions.

C plans stay in c_api. Persistent pages and catalog use contracts, with C's
framed WAL and transaction manager. No ReferencePageStore is instantiated.
"""
from __future__ import annotations

import copy
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .. import contracts as a
from .. import c_api as c
from ..execution import QueryExecutor
from ..index import IndexManager
from ..models import Column, Record, RecordId, TableSchema
from ..module_a import StorageService
from ..module_a.pages import encode
from ..transactions import TransactionManager, WAL


@contextmanager
def boundary():
    try:
        yield
    except a.DBError as exc:
        raise c.DBError(exc.code, exc.message, position=exc.position,
                        transaction_effect=exc.transaction_effect) from exc


def page_from_dict(value):
    if value is None:
        return None
    return a.Page(a.PageKey(**value["key"]), value["kind"], value["page_lsn"], value["payload"])


class LogAdapter:
    def __init__(self, wal, transactions):
        self.wal, self.transactions = wal, transactions

    def append_log(self, record):
        with self.transactions._condition:
            tx = self.transactions.get_transaction(record.tx_id)
            raw = asdict(record)
            raw["prev_lsn"] = tx.last_lsn
            lsn = self.wal.append_log(raw)
            self.transactions.transactions[tx.id].last_lsn = lsn
            return lsn

    def ensure_log_durable(self, lsn):
        self.wal.ensure_log_durable(lsn)

    def get_log_record(self, lsn):
        if type(lsn) is not int or not 1 <= lsn <= len(self.wal.records):
            raise a.DBError("WAL_NOT_FOUND", "Unknown LSN")
        raw = copy.deepcopy(self.wal.get(lsn))
        change = raw.get("change")
        if change:
            before, after = change["before_image"], change["after_image"]
            # C persists a reversed CLR; A expects the original UPDATE images.
            if raw["kind"] == "CLR":
                before, after = after, before
            change = a.PageChange(a.PageKey(**change["key"]), page_from_dict(before), page_from_dict(after))
        return a.LogRecord(raw.get("tx_id"), raw["kind"], lsn, raw.get("prev_lsn"),
                           change, raw.get("undo_next_lsn"), raw.get("checkpoint_data"))


class Transactions(TransactionManager):
    def recover(self):
        if not self.store.recovering:
            raise c.DBError("RECOVERY_MODE_REQUIRED", "Close and reopen ContractRuntime for recovery")
        return super().recover()

    def has_active_transactions(self):
        with self._condition:
            return any(tx.state == "ACTIVE" for tx in self.transactions.values())

    def acquire_lock(self, tx_id, resource, mode, timeout_ms=2000):
        if resource == "CATALOG":
            resource = "catalog:schema"
        return super().acquire_lock(tx_id, resource, mode, timeout_ms)

    def write_change(self, tx_id, change):
        # C's index manager uses this entry; records use A's prepare API directly.
        with boundary():
            self.acquire_lock(tx_id, "storage:write", "X")
            return self.store.write_index_change(tx_id, change)


class PageAdapter:
    def __init__(self):
        self.storage = self.transactions = self.log = None
        self.recovering = True

    def read_image(self, key, metrics=None):
        with boundary():
            if key.file_id == "ci_catalog":
                values = {}
                for obj in self.storage.list_objects():
                    if obj.kind == "TABLE":
                        table = self.storage.get_table(obj.id)
                        names = {column.id: column.name for column in table.columns}
                        for index in table.indexes:
                            values[index.id] = dict(id=index.id, name=index.name, table_id=table.name,
                                                    column_ids=[names[i] for i in index.column_ids],
                                                    unique=index.unique, root_page_id=index.root_page_id)
                return dict(kind="INDEX", payload=values)
            try:
                handle = self.storage.fetch_page(a.PageKey(key.file_id, key.page_id), "READ", metrics or a.Metrics())
            except a.DBError as exc:
                if exc.code in {"OBJECT_NOT_FOUND", "PAGE_NOT_FOUND"}:
                    return None
                raise
            try:
                payload = handle.page.payload
                return copy.deepcopy(payload.get("c_image"))
            finally:
                self.storage.release_page(handle)

    def validate_image(self, image):
        with boundary():
            encode(a.Page(a.PageKey("ci_" + "f" * 32, 999999), "INDEX", 2**63 - 1, {"c_image": image}))

    def write_index_change(self, tx_id, change):
        tx = self.transactions.get_transaction(tx_id)
        if self.read_image(change.key) != change.before_image:
            raise c.DBError("STALE_PAGE", "Index before-image no longer matches")
        if change.key.file_id == "ci_catalog":
            previous = change.before_image["payload"]
            values = change.after_image["payload"]
            if not previous.keys() <= values.keys():
                raise c.DBError("UNSUPPORTED_COMMAND", "Index deletion is not exposed by this driver")
            for identity, info in values.items():
                if previous.get(identity) == info:
                    continue
                table = self.storage.get_table(info["table_id"])
                columns = {column.name: column.id for column in table.columns}
                if identity in previous:
                    self.storage.update_index_root(identity, info["root_page_id"], tx)
                else:
                    index = a.IndexInfo(identity, info["name"], table.id,
                                        [columns[name] for name in info["column_ids"]], info["unique"], info["root_page_id"])
                    self.storage.apply_catalog_command(a.CatalogCommand("CREATE_INDEX", "sql-user", index=index, transaction=tx))
            return self.transactions.get_transaction(tx_id).last_lsn
        key = a.PageKey(change.key.file_id, change.key.page_id)
        try:
            count = self.storage.file_page_count(key.file_id)
        except a.DBError as exc:
            if exc.code != "OBJECT_NOT_FOUND":
                raise
            self.storage.register_index_file(key.file_id, tx)
            count = 0
        while count <= key.page_id:
            self.storage.allocate_page(key.file_id, tx)
            count += 1
        handle = self.storage.fetch_page(key, "READ", a.Metrics())
        try:
            before = handle.page
        finally:
            self.storage.release_page(handle)
        after = a.Page(key, "INDEX", before.page_lsn, {"c_image": copy.deepcopy(change.after_image)})
        ctx = a.ExecutionContext(uuid.uuid4().hex, tx.session_id, "sql-user", tx.id)
        prepared = self.storage.prepare_index_changes([a.PageChange(key, before, after)], ctx)
        return self.publish_for(tx_id, prepared)

    def publish_for(self, tx_id, prepared):
        # Runtime serializes request admission; do not keep per-request state here.
        lsns = []
        try:
            for change in prepared.changes:
                lsns.append(self.log.append_log(a.LogRecord(tx_id, "UPDATE", change=change)))
        except BaseException:
            self.storage.discard_changes(prepared)
            raise
        self.storage.apply_changes(prepared, lsns)
        return lsns[-1]

    def apply_image(self, key, image, lsn):
        with boundary():
            record = self.log.get_log_record(lsn)
            if self.recovering:
                self.storage.apply_recovery_change(record.change, lsn, "REDO")
            else:
                tx = self.transactions.get_transaction(record.tx_id)
                self.storage.apply_rollback_change(record.change, lsn, tx)

    def flush_all(self):
        with boundary():
            if not self.recovering:
                self.storage.flush_all()

    def sync_files(self, file_ids=None):
        with boundary():
            if file_ids is None:
                self.storage.sync_all_files()
            else:
                self.storage.sync_files(file_ids)

    def reset_cache(self):
        with boundary(), self.storage.exclusive_experiment():
            self.storage.reset_cache()


class CatalogAdapter:
    def __init__(self, storage):
        self.storage = storage

    def table(self, name):
        with boundary():
            table = self.storage.get_table(name)
            names = {column.id: column.name for column in table.columns}
            primary = next((names[rule.columns[0]] for rule in table.constraints if rule.kind == "PRIMARY_KEY"), None)
            return TableSchema(table.name, tuple(Column(x.name, x.type, x.nullable) for x in table.columns), primary)

    def tables(self):
        with boundary():
            return tuple(self.table(obj.id) for obj in self.storage.list_objects() if obj.kind == "TABLE")

    def constraints(self, name):
        with boundary():
            table = self.storage.get_table(name)
            names = {column.id: column.name for column in table.columns}
            result = []
            for rule in table.constraints:
                if rule.kind == "PRIMARY_KEY":
                    continue
                ref = self.storage.get_table(rule.ref_table) if rule.ref_table else None
                refs = {column.id: column.name for column in ref.columns} if ref else {}
                result.append(c.Constraint(rule.kind, [names[i] for i in rule.columns], ref.name if ref else None,
                                           [refs[i] for i in rule.ref_columns or []],
                                           c.expression_from_dict(rule.expression) if rule.expression else None))
            return result


class ContractIndexes(IndexManager):
    def _change(self, key, payload, ctx):
        before = self.store.read_image(key, ctx.metrics)
        after = None if payload is None else dict(kind="INDEX", payload=payload)
        self.store.validate_image(after)
        self.transactions.write_change(ctx.tx_id, c.PageChange(key, before, after))
        # A owns actual writeback accounting; dirtying a page is not a file write.


class Records:
    def __init__(self, runtime):
        self.runtime = runtime
        self.catalog = runtime.catalog

    def schema(self, table_id):
        return self.runtime.catalog.table(table_id)

    def constraints(self, table_id):
        return self.runtime.catalog.constraints(table_id)

    def related_tables(self, table_id):
        result = {table_id}
        for table in self.runtime.catalog.tables():
            for rule in self.constraints(table.name):
                if rule.kind == "FOREIGN_KEY" and (table.name == table_id or rule.ref_table == table_id):
                    result.update((table.name, rule.ref_table))
        return result

    @contextmanager
    def context(self, ctx):
        metrics = a.Metrics()
        with boundary():
            try:
                yield a.ExecutionContext(ctx.query_id, ctx.session_id, ctx.user_id, ctx.tx_id,
                                         metrics, ctx.memory_limit_bytes)
            finally:
                for key in ("data_page_reads", "index_page_reads", "page_writes", "cache_hits", "cache_misses", "evictions"):
                    setattr(ctx.metrics, key, getattr(ctx.metrics, key) + getattr(metrics, key))

    def scan(self, table_id, ctx):
        with self.context(ctx) as context:
            table = self.runtime.storage.get_table(table_id)
            for rid in self.runtime.storage.list_record_ids(table.id, context):
                row = self.runtime.storage.read_record(table.id, rid, context)
                yield Record(RecordId(rid.page_id, rid.slot_id), dict(zip([x.name for x in table.columns], row.values)))

    def fetch(self, table_id, rid, ctx):
        with self.context(ctx) as context:
            table = self.runtime.storage.get_table(table_id)
            row = self.runtime.storage.read_record(table.id, a.RID(rid.page_id, rid.slot_id), context)
            return Record(rid, dict(zip([x.name for x in table.columns], row.values)))

    def _write(self, table_id, operation, rid, values, ctx):
        self.runtime.transactions.acquire_lock(ctx.tx_id, "storage:write", "X", ctx.lock_timeout_ms)
        with self.context(ctx) as context:
            table = self.runtime.storage.get_table(table_id)
            schema = self.schema(table_id)
            if operation != "DELETE":
                values = schema.validate_record(values)
                if schema.primary_key:
                    for old in self.scan(table_id, ctx):
                        if old.rid != rid and old.values[schema.primary_key] == values[schema.primary_key]:
                            raise c.DBError("PRIMARY_KEY_VIOLATION", "Duplicate primary key")
            prepared = self.runtime.storage.prepare_record_change(
                table.id, operation, a.RID(rid.page_id, rid.slot_id) if rid else None,
                [values[column.name] for column in table.columns] if values is not None else None, context)
            self.runtime.pages.publish_for(ctx.tx_id, prepared)
            return RecordId(prepared.rid.page_id, prepared.rid.slot_id)

    def insert(self, table_id, values, ctx):
        return self._write(table_id, "INSERT", None, values, ctx)

    def update(self, table_id, rid, values, ctx):
        merged = self.fetch(table_id, rid, ctx).values
        merged.update(values)
        return self._write(table_id, "UPDATE", rid, merged, ctx)

    def delete(self, table_id, rid, ctx):
        return self._write(table_id, "DELETE", rid, None, ctx)


class ContractRuntime:
    """Single-process runtime; SqlSession admission is serialized, transactions use C locks."""
    def __init__(self, root, buffer_pages=16):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if (self.root / "c_pages_v1").exists():
            raise c.DBError("FORMAT_MISMATCH", "Use a new directory for integrated A storage")
        if (self.root / "a_storage" / "storage.json").exists() and not (self.root / "wal" / "contract.wal").exists():
            raise c.DBError("WAL_NOT_FOUND", "Existing A storage requires its persistent WAL")
        self.request_lock = threading.RLock()
        self.closed = False
        self.wal = None
        self._lockfile = (self.root / "contract_runtime.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._lockfile.seek(0, os.SEEK_END)
                if not self._lockfile.tell():
                    self._lockfile.write(b"0")
                    self._lockfile.flush()
                self._lockfile.seek(0)
                msvcrt.locking(self._lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.pages = PageAdapter()
            self.wal = WAL(self.root / "wal" / "contract.wal")
            self.transactions = Transactions(self.pages, self.wal)
            self.log = LogAdapter(self.wal, self.transactions)
            with boundary():
                self.storage = StorageService(self.root / "a_storage", self.log, self.transactions,
                                              capacity=max(2, buffer_pages), normalize_name=str.casefold,
                                              supported_constraints=frozenset({"PRIMARY_KEY", "UNIQUE", "CHECK", "FOREIGN_KEY"}))
            self.pages.storage, self.pages.transactions, self.pages.log = self.storage, self.transactions, self.log
            self.recovery_summary = self.transactions.recover()
            with boundary():
                self.storage.finish_recovery()
            self.pages.recovering = False
            self.catalog = CatalogAdapter(self.storage)
            self.records = Records(self)
            self.indexes = ContractIndexes(self.pages, self.records, self.transactions)
            self.executor = QueryExecutor(self.records, self.indexes, self.transactions)
        except BaseException:
            if self.wal:
                self.wal.close()
            self._lockfile.close()
            raise

    def create_table(self, schema, constraints=None):
        with self.request_lock, boundary():
            identity = "t_" + uuid.uuid4().hex
            columns = [a.ColumnDef("c_" + uuid.uuid4().hex, col.name, col.type, col.nullable) for col in schema.columns]
            ids = {col.name: col.id for col in columns}
            rules = [a.Constraint("PRIMARY_KEY", [ids[schema.primary_key]])] if schema.primary_key else []
            for rule in constraints or []:
                ref = (a.TableSchema(identity, schema.name, columns) if rule.ref_table == schema.name
                       else self.storage.get_table(rule.ref_table) if rule.ref_table else None)
                ref_ids = {col.name: col.id for col in ref.columns} if ref else {}
                rules.append(a.Constraint(rule.kind, [ids[name] for name in rule.columns], ref.id if ref else None,
                                          [ref_ids[name] for name in rule.ref_columns] if ref else None,
                                          asdict(rule.expression) if rule.expression else None))
            self.storage.apply_catalog_command(a.CatalogCommand("CREATE_TABLE", "sql-user",
                                               table=a.TableSchema(identity, schema.name, columns, rules)))

    def create_index(self, spec, session_id, user_id):
        with self.request_lock, boundary(), self.storage.catalog_transaction() as tx:
            ctx = c.ExecutionContext(uuid.uuid4().hex, tx.session_id, user_id, tx.id)
            # Scope owns rollback; do not use executor.create_index's second rollback.
            return self.indexes.create_index(spec, ctx)

    def context(self, session_id="local", user_id="test", memory_limit_bytes=64 * 1024 * 1024):
        tx = self.transactions.begin(session_id)
        return c.ExecutionContext(uuid.uuid4().hex, session_id, user_id, tx.id,
                                  memory_limit_bytes=memory_limit_bytes)

    def close(self):
        with self.request_lock:
            if self.closed:
                return
            try:
                if not self.transactions.needs_recovery:
                    for tx in list(self.transactions.transactions.values()):
                        if tx.state == "ACTIVE":
                            self.transactions.rollback(tx.id)
                    with boundary():
                        self.storage.close()
            finally:
                self.wal.close()
                self._lockfile.close()
                self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
