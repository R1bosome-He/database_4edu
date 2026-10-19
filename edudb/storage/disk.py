from __future__ import annotations

import json
import re
from pathlib import Path

from ..catalog import Catalog
from ..errors import CorruptionError, LegacyFormatError, NotFoundError, ValidationError
from ..fileio import atomic_write
from ..models import MAX_PAGE_ID, Page, identifier
from .page import PAGE_SIZE, decode_page, encode_page


class DiskManager:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.manifest_path = self.root / "storage.json"
        if self.manifest_path.exists():
            self._page_counts()
        elif any((self.root / "data").rglob("*.page")):
            raise LegacyFormatError(
                "Page allocation manifest is missing. For an intact M1 database, run "
                "python -m edudb migrate-storage --db <directory>"
            )
        else:
            self._save_counts({})

    def page_path(self, table: str, page_id: int) -> Path:
        if type(page_id) is not int or not 0 <= page_id <= MAX_PAGE_ID:
            raise ValidationError("Invalid page id")
        return self.root / "data" / identifier(table) / f"{page_id:08d}.page"

    def read_page(self, table: str, page_id: int) -> Page:
        path = self.page_path(table, page_id)
        if page_id >= self._page_counts().get(identifier(table), 0):
            raise NotFoundError(f"Unallocated page: {table}/{page_id}")
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise CorruptionError(f"Allocated page is missing: {table}/{page_id}") from exc
        return decode_page(raw, page_id)

    def write_page(self, table: str, page: Page) -> None:
        path = self.page_path(table, page.page_id)
        raw = encode_page(page)
        table = identifier(table)
        counts = self._page_counts()
        count = counts.get(table, 0)
        self._verify_files(table, count)
        if page.page_id > count:
            raise ValidationError("New pages must be appended in page-id order")
        atomic_write(path, raw)
        if page.page_id == count:
            try:
                self._save_counts({**counts, table: count + 1})
            except OSError:
                # Undo this unregistered append; existing pages are never removed.
                path.unlink()
                raise

    def page_ids(self, table: str) -> tuple[int, ...]:
        table = identifier(table)
        count = self._page_counts().get(table, 0)
        return self._verify_files(table, count)

    def _verify_files(self, table: str, count: int) -> tuple[int, ...]:
        ids = self._scan_ids(self.root, table)
        if len(ids) != count or any(page_id != index for index, page_id in enumerate(ids)):
            raise CorruptionError(f"Page allocation mismatch for {table}: expected {count} pages")
        return ids

    @staticmethod
    def _scan_ids(root: Path, table: str) -> tuple[int, ...]:
        directory = root / "data" / identifier(table)
        result = []
        for path in directory.glob("*.page"):
            if not path.is_file() or not re.fullmatch(r"[0-9]{8}\.page", path.name):
                raise CorruptionError(f"Invalid page filename: {path.name}")
            result.append(int(path.stem))
        return tuple(sorted(result))

    def _page_counts(self) -> dict[str, int]:
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if (data["format"] != "edudb.storage" or type(data["version"]) is not int
                    or data["version"] != 2 or not isinstance(data["page_counts"], dict)):
                raise ValidationError("Unsupported storage manifest")
            counts = data["page_counts"]
            for table, count in counts.items():
                if identifier(table) != table or type(count) is not int or not 0 <= count <= MAX_PAGE_ID + 1:
                    raise ValidationError("Invalid page allocation")
            return counts
        except (FileNotFoundError, ValueError, TypeError, KeyError) as exc:
            raise CorruptionError(f"Invalid or missing allocation manifest: {self.manifest_path}") from exc

    def _save_counts(self, counts: dict[str, int]) -> None:
        _write_manifest(self.manifest_path, counts)

    @classmethod
    def migrate_legacy(cls, root: str | Path) -> bool:
        """Validate an intact legacy directory, then add its allocation manifest."""
        root = Path(root)
        manifest_path = root / "storage.json"
        if manifest_path.exists():
            disk = cls(root)
            for table in disk._page_counts():
                disk.page_ids(table)
            return False
        if not (root / "catalog.json").is_file():
            raise NotFoundError("Legacy migration requires an existing catalog.json")
        catalog = Catalog(root)
        tables = {schema.name: schema for schema in catalog.tables()}
        data_dir = root / "data"
        if data_dir.exists():
            for entry in data_dir.iterdir():
                if not entry.is_dir() or entry.name not in tables:
                    raise CorruptionError(f"Unknown legacy data entry: {entry.name}")
        counts = {}
        for table, schema in tables.items():
            ids = cls._scan_ids(root, table)
            if any(page_id != index for index, page_id in enumerate(ids)):
                raise CorruptionError(f"Legacy page sequence contains a gap: {table}")
            primary_keys = set()
            for page_id in ids:
                path = data_dir / table / f"{page_id:08d}.page"
                page = decode_page(path.read_bytes(), page_id)
                for values in page.slots:
                    if values is not None:
                        record = schema.validate_record(values)
                        if schema.primary_key:
                            key = record[schema.primary_key]
                            if key in primary_keys:
                                raise CorruptionError(f"Duplicate legacy primary key: {table}")
                            primary_keys.add(key)
            counts[table] = len(ids)
        _write_manifest(manifest_path, counts)
        return True


def _write_manifest(path: Path, counts: dict[str, int]) -> None:
    data = {"format": "edudb.storage", "version": 2, "page_counts": counts}
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True).encode("utf-8"))
