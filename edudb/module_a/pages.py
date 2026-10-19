"""Private physical files for contract storage; writes are owned by StorageService."""
from __future__ import annotations

import hashlib
import json
import os
import struct
import zlib
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from ..contracts import DBError, Page, PageKey
from ..fileio import atomic_write

PAGE_SIZE = 4096
HEADER = struct.Struct(">4sIIQI")
CATALOG_KEY = PageKey("__catalog__", 0)


def validate_key(key: PageKey) -> None:
    if (not isinstance(key, PageKey) or not isinstance(key.file_id, str) or not key.file_id
            or len(key.file_id) > 128 or type(key.page_id) is not int or not 0 <= key.page_id < 2**32):
        raise DBError("INVALID_ARGUMENT", "Invalid page key")


def encode(page: Page) -> bytes:
    validate_key(page.key)
    if page.kind not in {"DATA", "INDEX"} or type(page.page_lsn) is not int or not 0 <= page.page_lsn < 2**64:
        raise DBError("INVALID_ARGUMENT", "Invalid page kind or LSN")
    try:
        payload = json.dumps(asdict(page), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DBError("INVALID_ARGUMENT", "Page payload must be finite JSON data") from exc
    if len(payload) > PAGE_SIZE - HEADER.size:
        raise DBError("PAGE_FULL", "Page exceeds 4096 bytes")
    return HEADER.pack(b"EDA3", page.key.page_id, len(payload), page.page_lsn, zlib.crc32(payload)) + payload + bytes(PAGE_SIZE - HEADER.size - len(payload))


def decode(raw: bytes, key: PageKey) -> Page:
    try:
        if len(raw) != PAGE_SIZE:
            raise ValueError("page size")
        magic, number, size, lsn, crc = HEADER.unpack_from(raw)
        payload = raw[HEADER.size:HEADER.size + size]
        if (magic != b"EDA3" or number != key.page_id or size > PAGE_SIZE - HEADER.size
                or zlib.crc32(payload) != crc or any(raw[HEADER.size + size:])):
            raise ValueError("page header")
        data = json.loads(payload)
        page = Page(PageKey(**data["key"]), data["kind"], data["page_lsn"], data["payload"])
        if page.key != key or page.page_lsn != lsn:
            raise ValueError("page identity")
        encode(page)
        return page
    except (ValueError, TypeError, KeyError, DBError, struct.error) as exc:
        raise DBError("CORRUPT_PAGE", f"Invalid page: {key}") from exc


class PageFiles:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, key: PageKey) -> Path:
        validate_key(key)
        name = hashlib.sha256(key.file_id.encode("utf-8")).hexdigest()
        return self.root / "pages" / name / f"{key.page_id:010d}.page"

    def read(self, key: PageKey, on_read: Callable[[], None] | None = None) -> Page | None:
        path = self.path(key)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        if on_read is not None:
            on_read()
        return decode(raw, key)

    def write(self, page: Page) -> None:
        atomic_write(self.path(page.key), encode(page))

    def sync(self, file_id: str) -> None:
        directory = self.path(PageKey(file_id, 0)).parent
        if directory.exists():
            for path in directory.glob("*.page"):
                with path.open("r+b") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
