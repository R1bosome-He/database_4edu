from __future__ import annotations

import json
import math
import struct
import zlib

from ..errors import CorruptionError, PageFullError, ValidationError
from ..models import MAX_PAGE_ID, Page

PAGE_SIZE = 4096
HEADER = struct.Struct(">4sB3xIII")
MAGIC = b"EDBP"
VERSION = 1


def validate_page(page: Page) -> None:
    if type(page.page_id) is not int or not 0 <= page.page_id <= MAX_PAGE_ID:
        raise ValidationError("Invalid page id")
    if not isinstance(page.slots, list):
        raise ValidationError("Page slots must be a list")
    for record in page.slots:
        if record is None:
            continue
        if not isinstance(record, dict) or any(not isinstance(k, str) for k in record):
            raise ValidationError("Invalid page record")
        for value in record.values():
            if value is not None and type(value) not in (str, int, float, bool):
                raise ValidationError("Records may only contain scalar values")
            if type(value) is float and not math.isfinite(value):
                raise ValidationError("Non-finite numbers are not supported")


def encode_page(page: Page) -> bytes:
    validate_page(page)
    payload = json.dumps(page.slots, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(payload) > PAGE_SIZE - HEADER.size:
        raise PageFullError(f"Page payload exceeds {PAGE_SIZE - HEADER.size} bytes")
    header = HEADER.pack(MAGIC, VERSION, page.page_id, len(payload), zlib.crc32(payload))
    return header + payload + bytes(PAGE_SIZE - len(header) - len(payload))


def decode_page(raw: bytes, expected_id: int) -> Page:
    if len(raw) != PAGE_SIZE:
        raise CorruptionError(f"Page must be exactly {PAGE_SIZE} bytes")
    magic, version, page_id, size, checksum = HEADER.unpack_from(raw)
    if magic != MAGIC or version != VERSION or page_id != expected_id:
        raise CorruptionError("Page header does not match its format or page id")
    if raw[5:8] != bytes(3) or size > PAGE_SIZE - HEADER.size:
        raise CorruptionError("Invalid page header")
    payload = raw[HEADER.size : HEADER.size + size]
    if zlib.crc32(payload) != checksum or any(raw[HEADER.size + size :]):
        raise CorruptionError("Page checksum or padding is invalid")
    try:
        page = Page(page_id, json.loads(payload.decode("utf-8")))
        validate_page(page)
    except (ValueError, TypeError) as exc:
        raise CorruptionError("Invalid page payload") from exc
    return page
