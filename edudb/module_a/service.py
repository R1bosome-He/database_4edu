from __future__ import annotations

import json
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from ..contracts import (
    CatalogCommand, ColumnDef, Constraint, DBError, ExecutionContext, IndexInfo,
    LogRecord, LogService, Metrics, ObjectInfo, Page, PageChange, PageHandle,
    PageKey, PreparedChange, Record, ResultColumn, RID, TableSchema, TableStats,
    Transaction, TransactionService, Value, ViewDef,
)
from ..fileio import atomic_write
from .pages import CATALOG_KEY, PageFiles, encode, validate_key


def api(function):
    @wraps(function)
    def guarded(self, *args, **kwargs):
        with self._mutex:
            try:
                return function(self, *args, **kwargs)
            except DBError:
                raise
            except OSError as exc:
                raise DBError("IO_ERROR", str(exc)) from exc
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                raise DBError("INVALID_ARGUMENT", str(exc)) from exc
    return guarded


@dataclass
class _Frame:
    page: Page | None
    dirty: bool = False
    handles: set[str] = field(default_factory=set)
    writer: str | None = None


@dataclass
class _Prepared:
    public: PreparedChange
    changes: list[PageChange]
    handles: list[PageHandle]
    tx: Transaction
    metrics: Metrics


class StorageService:
    """Contract A. All normal mutations require C's WAL and transaction services."""

    def __init__(
        self, root: str | Path, log: LogService, transactions: TransactionService,
        *, capacity: int = 16, normalize_name: Callable[[str], str] | None = None,
        supported_constraints: frozenset[str] = frozenset(),
    ) -> None:
        self._mutex = threading.RLock()
        if type(capacity) is not int or capacity < 2:
            raise DBError("INVALID_ARGUMENT", "Contract storage needs at least two buffer frames")
        for service, methods in ((log, ("append_log", "ensure_log_durable", "get_log_record")),
                                 (transactions, ("begin", "get_transaction", "acquire_lock", "commit", "rollback", "has_active_transactions"))):
            if any(not callable(getattr(service, name, None)) for name in methods):
                raise DBError("INVALID_ARGUMENT", "Missing required C service methods")
        self._root = Path(root)
        self._files = PageFiles(self._root)
        self._log = log
        self._transactions = transactions
        self._capacity = capacity
        self._normalize = normalize_name or (lambda value: value)
        self._constraints = supported_constraints
        self._frames: OrderedDict[PageKey, _Frame] = OrderedDict()
        self._handles: dict[str, tuple[PageHandle, PageKey, str]] = {}
        self._prepared: dict[str, _Prepared] = {}
        self._system_metrics = Metrics()
        self._mode = "RECOVERING"
        self._experiment_owner: int | None = None
        self._ddl_transactions: set[str] = set()
        try:
            marker = self._root / "storage.json"
            if marker.exists():
                data = json.loads(marker.read_text(encoding="utf-8"))
                if data != {"format": "edudb.contract-storage", "version": 3}:
                    raise DBError("FORMAT_MISMATCH", "Use a new directory for contract v1 storage; legacy data is preserved")
            else:
                if self._root.exists() and any(self._root.iterdir()):
                    raise DBError("FORMAT_MISMATCH", "A new contract database requires an empty directory")
                # Empty bootstrap contains no user object, allocation, or transaction.
                self._files.write(Page(CATALOG_KEY, "DATA", 0, self._empty_catalog()))
                atomic_write(marker, b'{"format":"edudb.contract-storage","version":3}')
        except OSError as exc:
            raise DBError("IO_ERROR", str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise DBError("CORRUPT_CATALOG", "Invalid storage marker") from exc

    @staticmethod
    def _empty_catalog() -> dict[str, Any]:
        return {"version": 0, "tables": {}, "views": {}, "owners": {}, "files": {}}

    def _online(self) -> None:
        if self._mode != "ONLINE":
            raise DBError("DATABASE_NOT_READY", f"Database state is {self._mode}")

    def _transaction(self, tx_id: str, session_id: str) -> Transaction:
        tx = self._transactions.get_transaction(tx_id, session_id)
        if tx.id != tx_id or tx.session_id != session_id or tx.state != "ACTIVE":
            raise DBError("INVALID_TRANSACTION", "An active transaction belonging to the session is required")
        return deepcopy(tx)

    def _context(self, ctx: ExecutionContext) -> Transaction:
        if not isinstance(ctx, ExecutionContext) or not isinstance(ctx.metrics, Metrics):
            raise DBError("INVALID_ARGUMENT", "ExecutionContext with Metrics is required")
        if type(ctx.memory_limit_bytes) is not int or ctx.memory_limit_bytes <= 0:
            raise DBError("RESOURCE_LIMIT", "Invalid memory budget")
        return self._transaction(ctx.tx_id, ctx.session_id)

    def _flush_frame(self, key: PageKey, metrics: Metrics) -> None:
        frame = self._frames[key]
        if not frame.dirty:
            return
        if frame.page is None or frame.page.page_lsn <= 0:
            raise DBError("WAL_REQUIRED", "A dirty page must carry a logged LSN")
        self._log.ensure_log_durable(frame.page.page_lsn)
        self._files.write(frame.page)
        frame.dirty = False
        if key != CATALOG_KEY:
            metrics.page_writes += 1

    def _frame(self, key: PageKey, metrics: Metrics, *, allow_missing: bool = False, kind: str = "DATA") -> _Frame:
        validate_key(key)
        if key in self._frames:
            if key != CATALOG_KEY:
                metrics.cache_hits += 1
            self._frames.move_to_end(key)
            return self._frames[key]
        if key != CATALOG_KEY:
            metrics.cache_misses += 1
        def count_read():
            if key != CATALOG_KEY:
                if kind == "INDEX":
                    metrics.index_page_reads += 1
                else:
                    metrics.data_page_reads += 1
        page = self._files.read(key, on_read=count_read)
        if page is None and not allow_missing:
            raise DBError("PAGE_NOT_FOUND", f"Missing allocated page: {key}")
        if page is not None and page.kind != kind:
            raise DBError("CORRUPT_PAGE", "Page kind does not match file metadata")
        if len(self._frames) >= self._capacity:
            victim = next((item for item, value in self._frames.items() if not value.handles), None)
            if victim is None:
                raise DBError("BUFFER_FULL", "All buffer frames are pinned")
            self._flush_frame(victim, metrics)
            del self._frames[victim]
            metrics.evictions += 1
        frame = _Frame(page)
        self._frames[key] = frame
        return frame

    def _pin(self, key: PageKey, mode: str, metrics: Metrics, *, allow_missing: bool = False, kind: str = "DATA") -> PageHandle:
        if mode not in {"READ", "WRITE"}:
            raise DBError("INVALID_ARGUMENT", "Page mode must be READ or WRITE")
        frame = self._frame(key, metrics, allow_missing=allow_missing, kind=kind)
        if frame.writer is not None or (mode == "WRITE" and frame.handles):
            raise DBError("PAGE_BUSY", "Page has conflicting fixed handles")
        token = uuid.uuid4().hex
        snapshot = deepcopy(frame.page) if frame.page else Page(key, kind, 0, None)
        handle = PageHandle(key, mode, snapshot, token)
        frame.handles.add(token)
        if mode == "WRITE":
            frame.writer = token
        self._handles[token] = (handle, key, mode)
        return handle

    def _release(self, handle: PageHandle) -> None:
        entry = self._handles.get(handle._token)
        if entry is None or entry[0] is not handle or not handle._active:
            raise DBError("INVALID_HANDLE", "Unknown, foreign, or released page handle")
        _, key, mode = entry
        if handle.key != key or handle.mode != mode:
            raise DBError("INVALID_HANDLE", "Handle metadata was modified")
        frame = self._frames[key]
        frame.handles.remove(handle._token)
        if frame.writer == handle._token:
            frame.writer = None
        del self._handles[handle._token]
        object.__setattr__(handle, "_active", False)

    def _catalog(self) -> dict[str, Any]:
        handle = self._pin(CATALOG_KEY, "READ", self._system_metrics)
        try:
            data = handle.page.payload
            if not isinstance(data, dict) or set(data) != {"version", "tables", "views", "owners", "files"}:
                raise DBError("CORRUPT_CATALOG", "Invalid catalog page")
            return data
        finally:
            self._release(handle)

    def _file(self, file_id: str, catalog: dict[str, Any] | None = None) -> dict[str, Any]:
        entry = (catalog or self._catalog())["files"].get(file_id)
        if entry is None or not entry["active"]:
            raise DBError("OBJECT_NOT_FOUND", f"Unknown active file: {file_id}")
        return entry

    def _lookup(self, mapping: dict[str, Any], name_or_id: str) -> Any:
        if not isinstance(name_or_id, str):
            raise DBError("INVALID_ARGUMENT", "Object name or ID must be a string")
        if name_or_id in mapping:
            return mapping[name_or_id]
        name = self._normalize(name_or_id)
        for value in mapping.values():
            if self._normalize(value["name"]) == name:
                return value
        raise DBError("OBJECT_NOT_FOUND", f"Unknown object: {name_or_id}")

    @staticmethod
    def _table(data: dict[str, Any]) -> TableSchema:
        return TableSchema(data["id"], data["name"], [ColumnDef(**c) for c in data["columns"]],
                           [Constraint(**c) for c in data["constraints"]],
                           [IndexInfo(**i) for i in data["indexes"]], data["version"])

    @staticmethod
    def _view(data: dict[str, Any]) -> ViewDef:
        copy = deepcopy(data)
        copy["output_columns"] = [ResultColumn(**column) for column in copy["output_columns"]]
        return ViewDef(**copy)

    @api
    def get_table(self, name_or_id: str) -> TableSchema:
        self._online()
        return self._table(self._lookup(self._catalog()["tables"], name_or_id))

    @api
    def get_view(self, name_or_id: str) -> ViewDef:
        self._online()
        return self._view(self._lookup(self._catalog()["views"], name_or_id))

    def _objects(self, catalog: dict[str, Any]) -> list[ObjectInfo]:
        result = []
        for kind, group in (("TABLE", "tables"), ("VIEW", "views")):
            for identity, obj in catalog[group].items():
                result.append(ObjectInfo(identity, obj["name"], kind, catalog["owners"][identity]))
                if kind == "TABLE":
                    for index in obj["indexes"]:
                        result.append(ObjectInfo(index["id"], index["name"], "INDEX", catalog["owners"][index["id"]]))
        return result

    @api
    def list_objects(self) -> list[ObjectInfo]:
        self._online()
        return self._objects(self._catalog())

    @property
    @api
    def catalog_version(self) -> int:
        with self._mutex:
            self._online()
            return self._catalog()["version"]

    @api
    def get_statistics(self, table_id: str) -> TableStats:
        self._online()
        catalog = self._catalog()
        if table_id not in catalog["tables"]:
            raise DBError("OBJECT_NOT_FOUND", "Statistics require a table ID")
        count = self._file(table_id, catalog)["count"]
        rows = 0
        for number in range(count):
            handle = self._pin(PageKey(table_id, number), "READ", self._system_metrics)
            try:
                rows += sum(values is not None for values in self._slots(handle.page))
            finally:
                self._release(handle)
        return TableStats(rows, count, None, datetime.now(timezone.utc).isoformat())

    def _new_object(self, catalog: dict[str, Any], identity: str, name: str) -> None:
        validate_key(PageKey(identity, 0))
        if identity.startswith("__") or not isinstance(name, str) or not name:
            raise DBError("INVALID_ARGUMENT", "Reserved ID or empty object name")
        if identity in catalog["owners"]:
            raise DBError("OBJECT_EXISTS", "Persistent object IDs cannot be reused")
        for obj in self._objects(catalog):
            if obj.id == identity or self._normalize(obj.name) == self._normalize(name):
                raise DBError("OBJECT_EXISTS", "Duplicate object ID or name")

    def _validate_table(self, table: TableSchema, catalog: dict[str, Any]) -> None:
        if not table.columns or table.indexes:
            raise DBError("INVALID_ARGUMENT", "Create a table with columns and publish indexes separately")
        ids = [c.id for c in table.columns]
        names = [self._normalize(c.name) for c in table.columns]
        existing = {c["id"] for schema in catalog["tables"].values() for c in schema["columns"]}
        if len(ids) != len(set(ids)) or len(names) != len(set(names)) or existing.intersection(ids):
            raise DBError("INVALID_ARGUMENT", "Column IDs must be globally unique and column names unambiguous")
        for column in table.columns:
            if (not isinstance(column.id, str) or not column.id or not column.name
                    or column.type not in {"INT", "FLOAT", "TEXT", "BOOL"} or type(column.nullable) is not bool):
                raise DBError("INVALID_ARGUMENT", "Invalid ColumnDef")
        for constraint in table.constraints:
            if constraint.kind not in self._constraints:
                raise DBError("UNSUPPORTED_CONSTRAINT", "Constraint capability has not been provided by C")
            if any(column not in ids for column in constraint.columns):
                raise DBError("INVALID_ARGUMENT", "Constraint references an unknown column ID")
            if constraint.kind == "FOREIGN_KEY":
                target = asdict(table) if constraint.ref_table == table.id else catalog["tables"].get(constraint.ref_table)
                if target is None or not constraint.ref_columns or len(constraint.columns) != len(constraint.ref_columns):
                    raise DBError("DEPENDENCY_NOT_FOUND", "Invalid foreign key target")
                target_ids = {c["id"] for c in target["columns"]}
                if any(column not in target_ids for column in constraint.ref_columns):
                    raise DBError("DEPENDENCY_NOT_FOUND", "Invalid foreign key columns")

    def _catalog_edit(self, command: CatalogCommand, catalog: dict[str, Any]) -> ObjectInfo:
        action = command.action
        if not isinstance(command.owner_id, str) or not command.owner_id:
            raise DBError("INVALID_ARGUMENT", "Catalog commands require an owner ID")
        if action == "CREATE_TABLE":
            table = command.table
            if not isinstance(table, TableSchema):
                raise DBError("INVALID_ARGUMENT", "CREATE_TABLE requires TableSchema")
            self._new_object(catalog, table.id, table.name)
            self._validate_table(table, catalog)
            if table.id in catalog["files"]:
                raise DBError("OBJECT_EXISTS", "Persistent file IDs cannot be reused")
            table = deepcopy(table)
            table.version = 1
            catalog["tables"][table.id] = asdict(table)
            catalog["files"][table.id] = dict(kind="DATA", count=0, active=True)
            result = ObjectInfo(table.id, table.name, "TABLE", command.owner_id)
        elif action == "CREATE_VIEW":
            view = command.view
            if not isinstance(view, ViewDef) or view.security_mode != "INVOKER" or view.owner_id != command.owner_id:
                raise DBError("INVALID_ARGUMENT", "Only owner-matched INVOKER views are supported")
            self._new_object(catalog, view.id, view.name)
            objects = {obj.id for obj in self._objects(catalog) if obj.kind in {"TABLE", "VIEW"}}
            if view.id in view.dependencies or not set(view.dependencies).issubset(objects):
                raise DBError("DEPENDENCY_NOT_FOUND", "View dependencies must refer to existing tables or views")
            catalog["views"][view.id] = asdict(view)
            result = ObjectInfo(view.id, view.name, "VIEW", command.owner_id)
        elif action == "CREATE_INDEX":
            index = command.index
            if not isinstance(index, IndexInfo):
                raise DBError("INVALID_ARGUMENT", "CREATE_INDEX requires IndexInfo")
            self._new_object(catalog, index.id, index.name)
            table = catalog["tables"].get(index.table_id)
            file = self._file(index.id, catalog)
            if (table is None or len(index.column_ids) != 1 or index.column_ids[0] not in {c["id"] for c in table["columns"]}
                    or file["kind"] != "INDEX" or type(index.unique) is not bool or type(index.root_page_id) is not int
                    or not 0 <= index.root_page_id < file["count"]):
                raise DBError("INVALID_ARGUMENT", "Index requires one valid column and a built root page")
            table["indexes"].append(asdict(index))
            table["version"] += 1
            result = ObjectInfo(index.id, index.name, "INDEX", command.owner_id)
        elif action in {"DROP_TABLE", "DROP_VIEW", "DROP_INDEX"}:
            identity = command.object_id
            result = next((obj for obj in self._objects(catalog) if obj.id == identity), None)
            if result is None or result.kind != action.removeprefix("DROP_"):
                raise DBError("OBJECT_NOT_FOUND", "Object ID or object kind does not match")
            if any(identity in view["dependencies"] for view in catalog["views"].values()):
                raise DBError("OBJECT_REFERENCED", "Object is referenced by a view")
            if any(constraint.get("ref_table") == identity for schema in catalog["tables"].values()
                   for constraint in schema["constraints"]):
                raise DBError("OBJECT_REFERENCED", "Object is referenced by a constraint")
            if result.kind == "TABLE":
                table = catalog["tables"][identity]
                if table["indexes"]:
                    raise DBError("OBJECT_REFERENCED", "Drop table indexes explicitly first")
                del catalog["tables"][identity]
                catalog["files"][identity]["active"] = False
            elif result.kind == "VIEW":
                del catalog["views"][identity]
            else:
                for table in catalog["tables"].values():
                    if any(index["id"] == identity for index in table["indexes"]):
                        table["indexes"] = [index for index in table["indexes"] if index["id"] != identity]
                        table["version"] += 1
                catalog["files"][identity]["active"] = False
            # Retain retired IDs so a later object cannot inherit old references.
            catalog["version"] += 1
            return result
        else:
            raise DBError("UNSUPPORTED_COMMAND", f"Unsupported catalog command: {action}")
        catalog["owners"][result.id] = command.owner_id
        catalog["version"] += 1
        return result

    @api
    def apply_catalog_command(self, command: CatalogCommand) -> ObjectInfo:
        self._online()
        if command.transaction is None:
            with self.catalog_transaction() as tx:
                nested = deepcopy(command)
                nested.transaction = tx
                return self.apply_catalog_command(nested)
        tx = self._transaction(command.transaction.id, command.transaction.session_id)
        if tx.id not in self._ddl_transactions:
            raise DBError("DDL_TRANSACTION_CONFLICT", "Use catalog_transaction, not a business transaction")
        before = self._catalog()
        after = deepcopy(before)
        result = self._catalog_edit(command, after)
        self._append_apply(self._prepare_catalog(before, after, tx))
        return result

    @contextmanager
    def catalog_transaction(self):
        with self._mutex:
            self._online()
            if self._transactions.has_active_transactions():
                raise DBError("DDL_TRANSACTION_CONFLICT", "DDL requires an independent idle transaction boundary")
            tx = self._transactions.begin("__ddl__:" + uuid.uuid4().hex)
            self._ddl_transactions.add(tx.id)
            try:
                try:
                    self._transactions.acquire_lock(tx.id, "CATALOG", "X", 5000)
                    yield deepcopy(tx)
                    if any(entry.tx.id == tx.id for entry in self._prepared.values()):
                        raise DBError("ACTIVE_RESOURCES", "Apply or discard prepared DDL changes before commit")
                except Exception as original:
                    for entry in list(self._prepared.values()):
                        if entry.tx.id == tx.id:
                            self._discard(entry.public)
                    try:
                        self._transactions.rollback(tx.id)
                    except Exception as rollback_error:
                        self._mode = "RECOVERING"
                        raise DBError("RECOVERY_REQUIRED", "DDL rollback failed; ordinary access is disabled", transaction_effect="TRANSACTION_ABORTED") from rollback_error
                    if isinstance(original, DBError):
                        original.transaction_effect = "TRANSACTION_ABORTED"
                        raise
                    raise DBError("DDL_FAILED", str(original), transaction_effect="TRANSACTION_ABORTED") from original
                try:
                    self._transactions.commit(tx.id)
                except Exception as exc:
                    # A cannot decide whether C's COMMIT reached durable storage.
                    self._mode = "RECOVERING"
                    raise DBError("COMMIT_UNCERTAIN", "C must resolve commit durability before ordinary access resumes") from exc
            finally:
                self._ddl_transactions.discard(tx.id)

    def _prepare_catalog(self, before: dict[str, Any], after: dict[str, Any], tx: Transaction) -> PreparedChange:
        frame = self._frame(CATALOG_KEY, self._system_metrics)
        if frame.page.payload != before:
            raise DBError("STALE_CATALOG", "Catalog changed before preparation")
        change = PageChange(CATALOG_KEY, deepcopy(frame.page), Page(CATALOG_KEY, "DATA", frame.page.page_lsn, after))
        return self._prepare([change], None, tx, self._system_metrics)

    def _prepare(self, changes: list[PageChange], rid: RID | None, tx: Transaction, metrics: Metrics) -> PreparedChange:
        if not changes or len({change.key for change in changes}) != len(changes):
            raise DBError("INVALID_ARGUMENT", "Prepared changes require unique page keys")
        handles = []
        try:
            for change in changes:
                image = change.after_image or change.before_image
                if image is None or image.key != change.key:
                    raise DBError("INVALID_ARGUMENT", "Change image/key mismatch")
                if change.after_image:
                    encode(change.after_image)
                handle = self._pin(change.key, "WRITE", metrics, allow_missing=change.before_image is None, kind=image.kind)
                handles.append(handle)
                if self._frames[change.key].page != change.before_image:
                    raise DBError("STALE_PAGE", "Page no longer matches before image")
            token = uuid.uuid4().hex
            public = PreparedChange(rid, deepcopy(changes), list(handles), token)
            self._prepared[token] = _Prepared(public, deepcopy(changes), handles, deepcopy(tx), metrics)
            return public
        except Exception:
            for handle in reversed(handles):
                self._release(handle)
            raise

    def _pending(self, prepared: PreparedChange) -> _Prepared:
        entry = self._prepared.get(prepared._token)
        if entry is None or entry.public is not prepared:
            raise DBError("INVALID_PREPARED_CHANGE", "Unknown, consumed, or foreign prepared change")
        return entry

    def _discard(self, prepared: PreparedChange) -> None:
        entry = self._pending(prepared)
        for handle in entry.handles:
            if handle._active:
                self._release(handle)
        del self._prepared[prepared._token]

    @api
    def discard_changes(self, prepared: PreparedChange) -> None:
        self._discard(prepared)

    def _append_apply(self, prepared: PreparedChange) -> None:
        entry = self._pending(prepared)
        lsns = []
        previous = self._transactions.get_transaction(entry.tx.id, entry.tx.session_id).last_lsn
        try:
            for change in entry.changes:
                lsn = self._log.append_log(LogRecord(entry.tx.id, "UPDATE", prev_lsn=previous, change=deepcopy(change)))
                lsns.append(lsn)
                previous = lsn
            self.apply_changes(prepared, lsns)
        except Exception:
            if prepared._token in self._prepared:
                self._discard(prepared)
            raise

    @api
    def apply_changes(self, prepared: PreparedChange, lsns: list[int]) -> None:
        self._online()
        entry = self._pending(prepared)
        try:
            self._transaction(entry.tx.id, entry.tx.session_id)
            if prepared.changes != entry.changes or prepared.handles != entry.handles:
                raise DBError("INVALID_PREPARED_CHANGE", "Prepared changes were modified")
            if len(lsns) != len(entry.changes) or len(set(lsns)) != len(lsns):
                raise DBError("WAL_MISMATCH", "One distinct WAL LSN is required per change")
            pages = []
            for change, lsn, handle in zip(entry.changes, lsns, entry.handles):
                if type(lsn) is not int or lsn <= 0:
                    raise DBError("WAL_MISMATCH", "Invalid WAL LSN")
                log = self._log.get_log_record(lsn)
                if log.lsn != lsn or log.tx_id != entry.tx.id or log.kind != "UPDATE" or log.change != change:
                    raise DBError("WAL_MISMATCH", "WAL does not describe this transaction/page change")
                if self._frames[change.key].page != change.before_image or not handle._active:
                    raise DBError("STALE_PAGE", "Before image or fixed handle is no longer valid")
                if change.before_image and lsn <= change.before_image.page_lsn:
                    raise DBError("WAL_MISMATCH", "WAL LSN must advance page_lsn")
                source = change.after_image or Page(change.key, change.before_image.kind, lsn, None)
                page = deepcopy(source)
                page.page_lsn = lsn
                encode(page)
                pages.append(page)
            # Validation/encoding happens for the entire batch before publication.
            for page in pages:
                frame = self._frames[page.key]
                frame.page = page
                frame.dirty = True
        finally:
            self._discard(prepared)

    @api
    def file_page_count(self, file_id: str) -> int:
        self._online()
        return self._file(file_id)["count"]

    @api
    def sync_all_files(self) -> None:
        self.sync_files([CATALOG_KEY.file_id, *self._catalog()["files"]])

    @api
    def update_index_root(self, index_id: str, root_page_id: int, tx: Transaction) -> None:
        self._online()
        tx = self._transaction(tx.id, tx.session_id)
        before = self._catalog()
        file = self._file(index_id, before)
        if type(root_page_id) is not int or not 0 <= root_page_id < file["count"]:
            raise DBError("INVALID_ARGUMENT", "Index root must be allocated")
        after = deepcopy(before)
        for table in after["tables"].values():
            for index in table["indexes"]:
                if index["id"] == index_id:
                    if index["root_page_id"] == root_page_id:
                        return
                    index["root_page_id"] = root_page_id
                    table["version"] += 1
                    after["version"] += 1
                    self._append_apply(self._prepare_catalog(before, after, tx))
                    return
        raise DBError("OBJECT_NOT_FOUND", "Unknown index")

    @api
    def register_index_file(self, file_id: str, tx: Transaction) -> None:
        self._online()
        tx = self._transaction(tx.id, tx.session_id)
        validate_key(PageKey(file_id, 0))
        before = self._catalog()
        if file_id.startswith("__") or file_id in before["files"] or file_id in before["owners"]:
            raise DBError("OBJECT_EXISTS", "Index file ID must be new")
        after = deepcopy(before)
        after["files"][file_id] = dict(kind="INDEX", count=0, active=True)
        self._append_apply(self._prepare_catalog(before, after, tx))

    def _allocation_changes(self, file_id: str, payload: Any, tx: Transaction, metrics: Metrics) -> tuple[PageKey, list[PageChange]]:
        catalog = self._catalog()
        file = self._file(file_id, catalog)
        key = PageKey(file_id, file["count"])
        current = self._frame(key, metrics, allow_missing=True, kind=file["kind"]).page
        if current and current.payload is not None:
            raise DBError("CORRUPT_PAGE", "Unregistered live page at allocation position")
        after_catalog = deepcopy(catalog)
        after_catalog["files"][file_id]["count"] += 1
        catalog_page = self._frame(CATALOG_KEY, self._system_metrics).page
        changes = [
            PageChange(key, deepcopy(current), Page(key, file["kind"], current.page_lsn if current else 0, payload)),
            PageChange(CATALOG_KEY, deepcopy(catalog_page), Page(CATALOG_KEY, "DATA", catalog_page.page_lsn, after_catalog)),
        ]
        return key, changes

    @api
    def allocate_page(self, file_id: str, tx: Transaction) -> PageKey:
        self._online()
        tx = self._transaction(tx.id, tx.session_id)
        file = self._file(file_id)
        payload = {"slots": []} if file["kind"] == "DATA" else {}
        key, changes = self._allocation_changes(file_id, payload, tx, self._system_metrics)
        self._append_apply(self._prepare(changes, None, tx, self._system_metrics))
        return key

    @api
    def fetch_page(self, key: PageKey, mode: str, metrics: Metrics) -> PageHandle:
        self._online()
        if key == CATALOG_KEY:
            raise DBError("INVALID_ARGUMENT", "Catalog pages are private")
        file = self._file(key.file_id)
        if type(key.page_id) is not int or not 0 <= key.page_id < file["count"]:
            raise DBError("PAGE_NOT_FOUND", "Page is not allocated")
        return self._pin(key, mode, metrics, kind=file["kind"])

    @api
    def release_page(self, handle: PageHandle) -> None:
        if any(handle in entry.handles for entry in self._prepared.values()):
            raise DBError("PAGE_BUSY", "Release prepared handles through apply_changes or discard_changes")
        self._release(handle)

    @staticmethod
    def _slots(page: Page) -> list[Any]:
        if page.kind != "DATA" or not isinstance(page.payload, dict) or not isinstance(page.payload.get("slots"), list):
            raise DBError("CORRUPT_PAGE", "Invalid data page slots")
        return page.payload["slots"]

    @staticmethod
    def _rid(rid: RID) -> None:
        if not isinstance(rid, RID) or type(rid.page_id) is not int or type(rid.slot_id) is not int or min(rid.page_id, rid.slot_id) < 0:
            raise DBError("RECORD_NOT_FOUND", "Invalid RID")

    @api
    def read_record(self, table_id: str, rid: RID, ctx: ExecutionContext) -> Record:
        self._online()
        self._context(ctx)
        self._rid(rid)
        table = self.get_table(table_id)
        if table.id != table_id:
            raise DBError("INVALID_ARGUMENT", "Record access requires a table ID")
        try:
            handle = self.fetch_page(PageKey(table_id, rid.page_id), "READ", ctx.metrics)
        except DBError as exc:
            if exc.code == "PAGE_NOT_FOUND":
                raise DBError("RECORD_NOT_FOUND", "RID page does not exist") from exc
            raise
        try:
            slots = self._slots(handle.page)
            if rid.slot_id >= len(slots) or slots[rid.slot_id] is None:
                raise DBError("RECORD_NOT_FOUND", "RID slot does not exist")
            return Record(rid, deepcopy(slots[rid.slot_id]))
        finally:
            self._release(handle)

    def list_record_ids(self, table_id: str, ctx: ExecutionContext) -> Iterator[RID]:
        self._scan_start(table_id, ctx)
        count = self._scan_count(table_id, ctx)
        for number in range(count):
            ids = self._page_record_ids(table_id, number, ctx)
            yield from ids

    @api
    def _scan_start(self, table_id: str, ctx: ExecutionContext) -> None:
        self._online()
        self._context(ctx)
        if self.get_table(table_id).id != table_id:
            raise DBError("INVALID_ARGUMENT", "Record iteration requires a table ID")

    @api
    def _scan_count(self, table_id: str, ctx: ExecutionContext) -> int:
        self._context(ctx)
        return self._file(table_id)["count"]

    @api
    def _page_record_ids(self, table_id: str, number: int, ctx: ExecutionContext) -> list[RID]:
        self._online()
        self._context(ctx)
        handle = self.fetch_page(PageKey(table_id, number), "READ", ctx.metrics)
        try:
            return [RID(number, slot) for slot, values in enumerate(self._slots(handle.page)) if values is not None]
        finally:
            self._release(handle)

    def _values(self, table: TableSchema, values: list[Value]) -> list[Value]:
        if not isinstance(values, list) or len(values) != len(table.columns):
            raise DBError("INVALID_ARGUMENT", "Record values must follow the schema's column order")
        if any(value is not None and type(value) not in (str, int, float, bool) for value in values):
            raise DBError("INVALID_ARGUMENT", "Record values must be scalar")
        return deepcopy(values)

    @api
    def prepare_record_change(
        self, table_id: str, operation: str, rid: RID | None = None,
        values: list[Value] | None = None, ctx: ExecutionContext | None = None,
    ) -> PreparedChange:
        self._online()
        tx = self._context(ctx)
        table = self.get_table(table_id)
        if table.id != table_id:
            raise DBError("INVALID_ARGUMENT", "Record changes require a table ID")
        if operation not in {"INSERT", "UPDATE", "DELETE"}:
            raise DBError("INVALID_ARGUMENT", "Unsupported record operation")
        if operation != "DELETE":
            values = self._values(table, values)
        count = self._file(table_id)["count"]
        if operation == "INSERT":
            if rid is not None:
                raise DBError("INVALID_ARGUMENT", "INSERT assigns the RID")
            if count:
                key = PageKey(table_id, count - 1)
                handle = self.fetch_page(key, "READ", ctx.metrics)
                try:
                    before = handle.page
                    after = deepcopy(before)
                    slot = len(self._slots(after))
                    after.payload["slots"].append(values)
                finally:
                    self._release(handle)
                try:
                    encode(after)
                    changes = [PageChange(key, before, after)]
                    target = RID(key.page_id, slot)
                except DBError as exc:
                    if exc.code != "PAGE_FULL":
                        raise
                    key, changes = self._allocation_changes(table_id, {"slots": [values]}, tx, ctx.metrics)
                    target = RID(key.page_id, 0)
            else:
                key, changes = self._allocation_changes(table_id, {"slots": [values]}, tx, ctx.metrics)
                target = RID(key.page_id, 0)
        else:
            self._rid(rid)
            self.read_record(table_id, rid, ctx)
            key = PageKey(table_id, rid.page_id)
            handle = self.fetch_page(key, "READ", ctx.metrics)
            try:
                before = handle.page
                after = deepcopy(before)
                after.payload["slots"][rid.slot_id] = None if operation == "DELETE" else values
            finally:
                self._release(handle)
            changes = [PageChange(key, before, after)]
            target = rid
        size = sum(len(encode(change.after_image)) + (len(encode(change.before_image)) if change.before_image else 0) for change in changes)
        if size > ctx.memory_limit_bytes:
            raise DBError("RESOURCE_LIMIT", "Prepared page images exceed the query memory budget")
        return self._prepare(changes, target, tx, ctx.metrics)

    @api
    def prepare_index_changes(self, changes: list[PageChange], ctx: ExecutionContext) -> PreparedChange:
        self._online()
        tx = self._context(ctx)
        for change in changes:
            file = self._file(change.key.file_id)
            if (file["kind"] != "INDEX" or change.key.page_id >= file["count"]
                    or change.before_image is None or change.after_image is None
                    or change.after_image.kind != "INDEX"):
                raise DBError("INVALID_ARGUMENT", "Index changes require allocated INDEX pages")
        size = sum(len(encode(c.before_image)) + len(encode(c.after_image)) for c in changes)
        if size > ctx.memory_limit_bytes:
            raise DBError("RESOURCE_LIMIT", "Index images exceed memory budget")
        return self._prepare(changes, None, tx, ctx.metrics)

    @api
    def flush_page(self, key: PageKey) -> None:
        self._online()
        validate_key(key)
        if key != CATALOG_KEY:
            file = self._file(key.file_id)
            if key.page_id >= file["count"]:
                raise DBError("PAGE_NOT_FOUND", "Page is not allocated")
        frame = self._frames.get(key)
        if frame is not None:
            if frame.handles:
                raise DBError("PAGE_PINNED", "Release fixed handles before flushing this page")
            self._flush_frame(key, self._system_metrics)

    @api
    def flush_all(self) -> None:
        self._online()
        for key in list(self._frames):
            if not self._frames[key].handles:
                self._flush_frame(key, self._system_metrics)

    @contextmanager
    def exclusive_experiment(self):
        with self._mutex:
            self._online()
            if self._experiment_owner is not None or self._transactions.has_active_transactions():
                raise DBError("EXPERIMENT_BUSY", "Experiment requires no active transactions")
            self._experiment_owner = threading.get_ident()
            try:
                yield
            finally:
                self._experiment_owner = None

    @api
    def reset_cache(self) -> None:
        self._online()
        if self._experiment_owner != threading.get_ident() or self._transactions.has_active_transactions():
            raise DBError("EXPERIMENT_REQUIRED", "Cache reset requires an exclusive idle experiment")
        if self._handles:
            raise DBError("PAGE_PINNED", "Cache reset cannot invalidate fixed handles")
        self.flush_all()
        self._frames.clear()

    def _replay(self, change: PageChange, lsn: int, direction: str) -> None:
        if direction not in {"REDO", "UNDO"} or type(lsn) is not int or lsn <= 0:
            raise DBError("INVALID_ARGUMENT", "Invalid recovery direction or LSN")
        record = self._log.get_log_record(lsn)
        if record.change != change or record.lsn != lsn:
            raise DBError("WAL_MISMATCH", "Recovery record does not match its page change")
        if direction == "UNDO" and record.kind != "CLR":
            raise DBError("WAL_MISMATCH", "UNDO requires the durable progress LSN of a CLR")
        if direction == "REDO" and record.kind not in {"UPDATE", "CLR"}:
            raise DBError("WAL_MISMATCH", "REDO requires UPDATE or CLR")
        undo = direction == "UNDO" or record.kind == "CLR"
        image = change.before_image if undo else change.after_image
        reference = change.after_image or change.before_image
        if (reference is None or any(p.key != change.key or p.kind != reference.kind
                                     for p in (change.before_image, change.after_image) if p is not None)):
            raise DBError("INVALID_ARGUMENT", "Invalid recovery image")
        frame = self._frame(change.key, self._system_metrics, allow_missing=True, kind=reference.kind)
        if frame.handles:
            raise DBError("PAGE_PINNED", "Recovery cannot replace a fixed page")
        if frame.page is not None and frame.page.page_lsn >= lsn:
            return
        page = deepcopy(image) if image else Page(change.key, reference.kind, lsn, None)
        page.page_lsn = lsn
        encode(page)
        self._log.ensure_log_durable(lsn)
        frame.page = page
        frame.dirty = True
        self._flush_frame(change.key, self._system_metrics)

    @api
    def apply_recovery_change(self, change: PageChange, lsn: int, direction: str) -> None:
        if self._mode != "RECOVERING":
            raise DBError("RECOVERY_MODE_REQUIRED", "Startup recovery must precede ordinary access")
        self._replay(change, lsn, direction)

    @api
    def apply_rollback_change(self, change: PageChange, lsn: int, tx: Transaction) -> None:
        if self._mode not in {"ONLINE", "RECOVERING"}:
            raise DBError("DATABASE_NOT_READY", "Database is closed")
        self._transaction(tx.id, tx.session_id)
        record = self._log.get_log_record(lsn)
        if record.tx_id != tx.id:
            raise DBError("WAL_MISMATCH", "CLR belongs to another transaction")
        self._replay(change, lsn, "UNDO")

    @api
    def finish_recovery(self) -> None:
        if self._mode != "RECOVERING" or self._handles or self._transactions.has_active_transactions():
            raise DBError("RECOVERY_INCOMPLETE", "Recovery needs all transactions and fixed pages resolved")
        catalog = self._catalog()
        for file_id, file in catalog["files"].items():
            if file["active"]:
                for number in range(file["count"]):
                    frame = self._frame(PageKey(file_id, number), self._system_metrics, kind=file["kind"])
                    if frame.page.payload is None:
                        raise DBError("CORRUPT_PAGE", "Allocated page is a tombstone")
        self._mode = "ONLINE"

    @api
    def sync_files(self, file_ids: list[str]) -> None:
        if self._mode == "CLOSED":
            raise DBError("DATABASE_NOT_READY", "Database is closed")
        catalog = self._catalog()
        for file_id in file_ids:
            if file_id != CATALOG_KEY.file_id and file_id not in catalog["files"]:
                raise DBError("OBJECT_NOT_FOUND", "Unknown file ID")
        for file_id in file_ids:
            self._files.sync(file_id)

    @api
    def diagnostics(self) -> dict[str, Any]:
        return {
            "mode": self._mode, "cached_keys": list(self._frames),
            "pinned_handles": len(self._handles), "prepared_changes": len(self._prepared),
            "dirty_pages": sum(frame.dirty for frame in self._frames.values()),
            "system_metrics": asdict(self._system_metrics),
        }

    @api
    def close(self) -> None:
        if self._mode == "CLOSED":
            return
        if self._transactions.has_active_transactions() or self._handles:
            raise DBError("ACTIVE_RESOURCES", "Resolve transactions and fixed pages before shutdown")
        for key in list(self._frames):
            self._flush_frame(key, self._system_metrics)
        self._frames.clear()
        self._mode = "CLOSED"
