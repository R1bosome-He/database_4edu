"""Strict table-level 2PL plus physical REDO/UNDO and restartable CLR undo."""
from __future__ import annotations

import copy
import threading
import time
import uuid
from dataclasses import asdict

from ..c_api import DBError, PageChange, PageKey, PageStore, Transaction
from .wal import WAL


class TransactionManager:
    def __init__(self, store: PageStore, wal: WAL) -> None:
        self.store, self.wal = store, wal
        self._condition = threading.Condition(threading.RLock())
        self.transactions: dict[str, Transaction] = {}
        self._sessions: dict[str, str] = {}
        self._locks: dict[str, dict[str, str]] = {}
        self._operations: dict[str, int] = {}
        self.needs_recovery = False
        self.recovered = False

    def _ready(self) -> None:
        if not self.recovered or self.needs_recovery:
            raise DBError("RECOVERY_REQUIRED", "Run recovery before accepting requests")

    def begin(self, session_id: str) -> Transaction:
        with self._condition:
            self._ready()
            if session_id in self._sessions:
                raise DBError("TRANSACTION_ACTIVE", "Session already has an active transaction")
            tx = Transaction(uuid.uuid4().hex, session_id)
            tx.last_lsn = self.wal.append_log(dict(kind="BEGIN", tx_id=tx.id, prev_lsn=None))
            self.transactions[tx.id] = tx
            self._sessions[session_id] = tx.id
            return copy.copy(tx)

    def get_transaction(self, tx_id: str, session_id: str | None = None) -> Transaction:
        with self._condition:
            self._ready()
            tx = self.transactions.get(tx_id)
            if tx is None or tx.state != "ACTIVE" or (session_id is not None and tx.session_id != session_id):
                raise DBError("INVALID_TRANSACTION", "Transaction is inactive or belongs to another session")
            return copy.copy(tx)

    def enter_operation(self, tx_id, session_id):
        with self._condition:
            self.get_transaction(tx_id, session_id)
            if tx_id in self._operations:
                raise DBError("TRANSACTION_BUSY", "Only one statement per transaction may execute at a time")
            self._operations[tx_id] = threading.get_ident()

    def leave_operation(self, tx_id):
        with self._condition:
            self._operations.pop(tx_id, None)

    def acquire_lock(self, tx_id: str, resource: str, mode: str, timeout_ms: int = 2000) -> None:
        if mode not in {"S", "X"} or timeout_ms < 0:
            raise DBError("INVALID_LOCK", "Expected S/X and nonnegative timeout")
        deadline = time.monotonic() + timeout_ms / 1000
        with self._condition:
            while True:
                self.get_transaction(tx_id)
                owners = self._locks.setdefault(resource, {})
                conflicts = [v for k, v in owners.items() if k != tx_id and (mode == "X" or v == "X")]
                if not conflicts:
                    owners[tx_id] = "X" if mode == "X" or owners.get(tx_id) == "X" else "S"
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.rollback(tx_id)
                    raise DBError("LOCK_TIMEOUT", f"Timed out waiting for {resource}",
                                  transaction_effect="TRANSACTION_ABORTED")
                self._condition.wait(min(remaining, .05))

    def write_change(self, tx_id: str, change: PageChange) -> int:
        with self._condition:
            tx = self.get_transaction(tx_id)
            # Locks on tables/catalog are owned by the caller; serialized apply prevents
            # log order from diverging from in-process page publication order.
            existing = self.store.read_image(change.key)
            if existing != change.before_image:
                raise DBError("STALE_PAGE", "Prepared before-image no longer matches")
            record = dict(kind="UPDATE", tx_id=tx_id, prev_lsn=tx.last_lsn, change=asdict(change))
            lsn = self.wal.append_log(record)
            self.transactions[tx_id].last_lsn = lsn
            try:
                # No dirty image is published before the WAL is durable.
                self.wal.ensure_log_durable(lsn)
                self.store.apply_image(change.key, change.after_image, lsn)
            except BaseException:
                self.needs_recovery = True
                raise
            return lsn

    def _end(self, tx: Transaction, state: str) -> None:
        tx.state = state
        self._sessions.pop(tx.session_id, None)
        for owners in self._locks.values():
            owners.pop(tx.id, None)
        self._condition.notify_all()

    def commit(self, tx_id: str) -> None:
        with self._condition:
            if tx_id in self._operations:
                raise DBError("TRANSACTION_BUSY", "Close/finish active statement before commit")
            tx = self.get_transaction(tx_id)
            lsn = self.wal.append_log(dict(kind="COMMIT", tx_id=tx_id, prev_lsn=tx.last_lsn))
            try:
                self.wal.ensure_log_durable(lsn)
            except BaseException:
                self.needs_recovery = True
                raise
            self.transactions[tx_id].last_lsn = lsn
            self._end(self.transactions[tx_id], "COMMITTED")

    def _undo(self, tx_id: str, last_lsn: int) -> int:
        count = 0
        cursor: int | None = last_lsn
        tail = last_lsn
        while cursor:
            record = self.wal.get(cursor)
            if record["kind"] == "CLR":
                cursor = record.get("undo_next_lsn")
                continue
            previous = record.get("prev_lsn")
            if record["kind"] == "UPDATE":
                change = record["change"]
                undo = dict(key=change["key"], before_image=change["after_image"],
                            after_image=change["before_image"])
                tail = self.wal.append_log(dict(kind="CLR", tx_id=tx_id, prev_lsn=tail,
                                               undo_next_lsn=previous, change=undo))
                self.wal.ensure_log_durable(tail)
                self.store.apply_image(PageKey(**change["key"]), change["before_image"], tail)
                count += 1
            cursor = previous
        end = self.wal.append_log(dict(kind="ABORT", tx_id=tx_id, prev_lsn=tail))
        self.wal.ensure_log_durable(end)
        return count

    def rollback(self, tx_id: str) -> None:
        with self._condition:
            owner = self._operations.get(tx_id)
            if owner is not None and owner != threading.get_ident():
                raise DBError("TRANSACTION_BUSY", "Cannot roll back another executing statement")
            tx = self.get_transaction(tx_id)
            try:
                self._undo(tx_id, tx.last_lsn or 0)
            except BaseException:
                self.needs_recovery = True
                raise
            self._end(self.transactions[tx_id], "ABORTED")

    def recover(self) -> dict:
        with self._condition:
            if any(t.state == "ACTIVE" for t in self.transactions.values()) and not self.needs_recovery:
                raise DBError("ACTIVE_TRANSACTIONS", "Cannot recover over live transactions")
            started = time.perf_counter()
            latest: dict[str, int] = {}
            ended: set[str] = set()
            redone = undone = 0
            # Keep the entire WAL in v1. Checkpoints are durable markers, not permission
            # to truncate history: page LSNs make repeating REDO idempotent.
            try:
                for record in list(self.wal.records):
                    tx_id = record.get("tx_id")
                    if tx_id:
                        latest[tx_id] = record["lsn"]
                    if record["kind"] in {"COMMIT", "ABORT"}:
                        ended.add(tx_id)
                    if record["kind"] in {"UPDATE", "CLR"}:
                        change = record["change"]
                        self.store.apply_image(PageKey(**change["key"]), change["after_image"], record["lsn"])
                        redone += 1
                # Conflicting page writers hold X locks to end, so loser transactions
                # cannot overlap live page writes. Undo newest loser first.
                for tx_id in sorted(latest, key=latest.get, reverse=True):
                    if tx_id not in ended:
                        undone += self._undo(tx_id, latest[tx_id])
                self.store.flush_all()
                self.store.sync_files()
            except BaseException:
                self.needs_recovery = True
                raise
            for tx in self.transactions.values():
                if tx.state == "ACTIVE":
                    tx.state = "ABORTED"
            self._sessions.clear()
            self._locks.clear()
            self._operations.clear()
            self.needs_recovery = False
            self.recovered = True
            self._condition.notify_all()
            return dict(redone=redone, undone=undone, duration_ms=(time.perf_counter() - started) * 1000)

    def checkpoint(self) -> None:
        with self._condition:
            self._ready()
            if self._sessions:
                raise DBError("ACTIVE_TRANSACTIONS", "Checkpoint requires quiescence; stop admission first")
            self.store.flush_all()
            self.store.sync_files()
            lsn = self.wal.append_log(dict(kind="CHECKPOINT", tx_id=None, prev_lsn=None,
                                           checkpoint_data={"active": [], "dirty": []}))
            self.wal.ensure_log_durable(lsn)
