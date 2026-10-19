"""Checksummed, length-framed WAL. A torn final frame is truncated on open."""
from __future__ import annotations

import json
import os
import struct
import threading
import zlib
from pathlib import Path
from typing import Any

from ..c_api import DBError

HEADER = struct.Struct(">II")
MAX_FRAME = 32 * 1024 * 1024


class WAL:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.records: list[dict[str, Any]] = []
        self.durable_lsn = 0
        self.failed = False
        self._file = self.path.open("a+b", buffering=0)
        try:
            self._read_frames()
        except BaseException:
            self._file.close()
            raise

    def _read_frames(self) -> None:
        self._file.seek(0)
        offset = 0
        while True:
            head = self._file.read(HEADER.size)
            if not head:
                break
            if len(head) != HEADER.size:
                self._truncate(offset)
                break
            size, checksum = HEADER.unpack(head)
            if not 0 < size <= MAX_FRAME:
                raise DBError("WAL_CORRUPTION", f"Invalid WAL frame at {offset}")
            raw = self._file.read(size)
            if len(raw) != size:
                self._truncate(offset)
                break
            # Complete frames with a bad checksum are corruption, not a torn tail.
            if zlib.crc32(raw) != checksum:
                raise DBError("WAL_CORRUPTION", f"WAL checksum mismatch at {offset}")
            try:
                record = json.loads(raw)
                if record["lsn"] != len(self.records) + 1:
                    raise ValueError("Non-contiguous LSN")
                if record["kind"] not in {"BEGIN", "UPDATE", "COMMIT", "ABORT", "CLR", "CHECKPOINT"}:
                    raise ValueError("Invalid log kind")
            except (ValueError, KeyError, TypeError) as exc:
                raise DBError("WAL_CORRUPTION", f"Invalid WAL at {offset}") from exc
            self.records.append(record)
            offset += HEADER.size + size
        self.durable_lsn = len(self.records)
        self._file.seek(0, os.SEEK_END)

    def _truncate(self, offset: int) -> None:
        self._file.truncate(offset)
        self._file.flush()
        os.fsync(self._file.fileno())

    def append_log(self, record_without_lsn: dict[str, Any]) -> int:
        with self._lock:
            if self.failed:
                raise DBError("WAL_FAILED", "WAL requires reopen/recovery")
            record = {**record_without_lsn, "lsn": len(self.records) + 1}
            raw = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(raw) > MAX_FRAME:
                raise DBError("LOG_TOO_LARGE", "WAL frame exceeds limit")
            frame = HEADER.pack(len(raw), zlib.crc32(raw)) + raw
            try:
                view = memoryview(frame)
                while view:
                    count = self._file.write(view)
                    if not count:
                        raise OSError("Short WAL write")
                    view = view[count:]
            except OSError:
                self.failed = True
                raise
            self.records.append(record)
            return record["lsn"]

    def ensure_log_durable(self, lsn: int) -> None:
        with self._lock:
            if self.failed or lsn > len(self.records) or lsn < 0:
                raise DBError("WAL_FAILED", "Cannot make requested log position durable")
            if lsn > self.durable_lsn:
                try:
                    self._file.flush()
                    os.fsync(self._file.fileno())
                except OSError:
                    self.failed = True
                    raise
                self.durable_lsn = len(self.records)

    def get(self, lsn: int) -> dict[str, Any]:
        return self.records[lsn - 1]

    def close(self) -> None:
        with self._lock:
            self._file.close()
