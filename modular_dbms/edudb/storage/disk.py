from __future__ import annotations

import re
from pathlib import Path

from ..errors import CorruptionError, NotFoundError, ValidationError
from ..fileio import atomic_write
from ..models import MAX_PAGE_ID, Page, identifier
from .page import PAGE_SIZE, decode_page, encode_page


class DiskManager:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def page_path(self, table: str, page_id: int) -> Path:
        if type(page_id) is not int or not 0 <= page_id <= MAX_PAGE_ID:
            raise ValidationError("Invalid page id")
        return self.root / "data" / identifier(table) / f"{page_id:08d}.page"

    def read_page(self, table: str, page_id: int) -> Page:
        path = self.page_path(table, page_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise NotFoundError(f"Missing page: {table}/{page_id}") from exc
        return decode_page(raw, page_id)

    def write_page(self, table: str, page: Page) -> None:
        path = self.page_path(table, page.page_id)
        raw = encode_page(page)
        atomic_write(path, raw)

    def page_ids(self, table: str) -> tuple[int, ...]:
        directory = self.root / "data" / identifier(table)
        result = []
        for path in directory.glob("*.page"):
            if not re.fullmatch(r"[0-9]{8}\.page", path.name):
                raise CorruptionError(f"Invalid page filename: {path.name}")
            result.append(int(path.stem))
        result.sort()
        if any(page_id != index for index, page_id in enumerate(result)):
            raise CorruptionError("Page sequence contains a gap")
        return tuple(result)
