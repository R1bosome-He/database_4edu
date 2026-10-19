"""In-memory test doubles for A's contract demo; NO persistent WAL or lock manager."""
from __future__ import annotations

from copy import deepcopy

from ..contracts import DBError, LogRecord, Transaction


class MemoryLog:
    def __init__(self) -> None:
        self.records: dict[int, LogRecord] = {}
        self.durable_lsn = 0
        self.events: list[tuple[str, int]] = []

    def append_log(self, record: LogRecord) -> int:
        lsn = len(self.records) + 1
        record = deepcopy(record)
        if record.prev_lsn is None:
            record.prev_lsn = next((number for number, previous in reversed(self.records.items())
                                    if previous.tx_id == record.tx_id), None)
        record.lsn = lsn
        self.records[lsn] = record
        self.events.append(("append", lsn))
        return lsn

    def get_log_record(self, lsn: int) -> LogRecord:
        if lsn not in self.records:
            raise DBError("WAL_NOT_FOUND", "Unknown LSN")
        return deepcopy(self.records[lsn])

    def ensure_log_durable(self, lsn: int) -> None:
        if lsn not in self.records:
            raise DBError("WAL_NOT_FOUND", "Cannot synchronize an unknown LSN")
        self.events.append(("ensure", lsn))
        self.durable_lsn = max(self.durable_lsn, lsn)


class MemoryTransactions:
    def __init__(self, log: MemoryLog) -> None:
        self.log = log
        self.transactions: dict[str, Transaction] = {}
        self.storage = None
        self.held_locks: dict[str, list[tuple[str, str]]] = {}

    def begin(self, session_id: str) -> Transaction:
        if any(tx.session_id == session_id and tx.state == "ACTIVE" for tx in self.transactions.values()):
            raise DBError("TRANSACTION_EXISTS", "Session already has an active transaction")
        tx = Transaction(f"tx-{len(self.transactions) + 1}", session_id)
        tx.last_lsn = self.log.append_log(LogRecord(tx.id, "BEGIN"))
        self.transactions[tx.id] = tx
        return deepcopy(tx)

    def get_transaction(self, tx_id: str, session_id: str) -> Transaction:
        tx = self.transactions.get(tx_id)
        if tx is None or tx.session_id != session_id or tx.state != "ACTIVE":
            raise DBError("INVALID_TRANSACTION", "Unknown active session transaction")
        records = [lsn for lsn, record in self.log.records.items() if record.tx_id == tx_id]
        tx.last_lsn = max(records)
        return deepcopy(tx)

    def acquire_lock(self, tx_id: str, resource: str, mode: str, timeout_ms: int) -> None:
        self.held_locks.setdefault(tx_id, []).append((resource, mode))

    def has_active_transactions(self) -> bool:
        return any(tx.state == "ACTIVE" for tx in self.transactions.values())

    def commit(self, tx_id: str) -> None:
        tx = self.transactions[tx_id]
        lsn = self.log.append_log(LogRecord(tx_id, "COMMIT"))
        self.log.ensure_log_durable(lsn)
        tx.state = "COMMITTED"
        tx.last_lsn = lsn
        self.held_locks.pop(tx_id, None)

    def rollback(self, tx_id: str) -> None:
        tx = self.transactions[tx_id]
        if tx.state != "ACTIVE":
            raise DBError("INVALID_TRANSACTION", "Cannot roll back an inactive transaction")
        cursor = self.get_transaction(tx_id, tx.session_id).last_lsn
        while cursor is not None:
            record = self.log.get_log_record(cursor)
            if record.kind == "CLR":
                self.storage.apply_rollback_change(record.change, record.lsn, deepcopy(tx))
                cursor = record.undo_next_lsn
                continue
            if record.kind == "UPDATE":
                lsn = self.log.append_log(LogRecord(tx_id, "CLR", change=record.change,
                                                   undo_next_lsn=record.prev_lsn))
                self.log.ensure_log_durable(lsn)
                self.storage.apply_rollback_change(record.change, lsn, deepcopy(tx))
            cursor = record.prev_lsn
        lsn = self.log.append_log(LogRecord(tx_id, "ABORT"))
        self.log.ensure_log_durable(lsn)
        tx.state = "ABORTED"
        tx.last_lsn = lsn
        self.held_locks.pop(tx_id, None)
