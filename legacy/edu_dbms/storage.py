from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Page, TableSchema


MAX_RECORDS_PER_PAGE = 64


class StorageManager:
    """Stores table pages as JSON files under the database directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.data_dir = root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def create_table(self, schema: TableSchema) -> None:
        self._table_dir(schema.name).mkdir(parents=True, exist_ok=True)

    def table_exists(self, table: str) -> bool:
        return self._table_dir(table).exists()

    def read_page(self, table: str, page_id: int) -> Page:
        path = self._page_path(table, page_id)
        if not path.exists():
            return Page(table.lower(), page_id, [])
        with path.open("r", encoding="utf-8") as fh:
            data: dict[str, Any] = json.load(fh)
        return Page(table.lower(), int(data["page_id"]), list(data.get("records", [])))

    def write_page(self, page: Page) -> None:
        table_dir = self._table_dir(page.table)
        table_dir.mkdir(parents=True, exist_ok=True)
        path = self._page_path(page.table, page.page_id)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(
                {"table": page.table, "page_id": page.page_id, "records": page.records},
                fh,
                ensure_ascii=False,
                indent=2,
            )
        tmp.replace(path)

    def _table_dir(self, table: str) -> Path:
        return self.data_dir / table.lower()

    def _page_path(self, table: str, page_id: int) -> Path:
        return self._table_dir(table) / f"page_{page_id:06d}.json"

