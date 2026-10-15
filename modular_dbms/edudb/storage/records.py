from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from ..catalog import Catalog
from ..errors import AlreadyExistsError, NotFoundError, PageFullError, ValidationError
from ..models import Page, Record, RecordId, Scalar, TableSchema, identifier
from .page import encode_page


class PageIO(Protocol):
    """M2 must preserve this interface, including returning detached pages."""

    def read_page(self, table: str, page_id: int) -> Page: ...

    def write_page(self, table: str, page: Page) -> None: ...

    def page_ids(self, table: str) -> tuple[int, ...]: ...


class RecordStore:
    def __init__(self, catalog: Catalog, pages: PageIO) -> None:
        self.catalog = catalog
        self.pages = pages

    def create_table(self, schema: TableSchema) -> None:
        self.catalog.create_table(schema)

    def insert(self, table: str, values: dict[str, Scalar]) -> RecordId:
        schema = self.catalog.table(table)
        record = schema.validate_record(values)
        encode_page(Page(0, [record]))
        self._check_primary_key(schema, record)
        ids = self.pages.page_ids(schema.name)
        page = self.pages.read_page(schema.name, ids[-1]) if ids else Page(0, [])
        page.slots.append(record)
        try:
            encode_page(page)
        except PageFullError:
            page = Page(page.page_id + 1, [record])
        self.pages.write_page(schema.name, page)
        return RecordId(page.page_id, len(page.slots) - 1)

    def scan(self, table: str) -> Iterator[Record]:
        schema = self.catalog.table(table)
        for page_id in self.pages.page_ids(schema.name):
            page = self.pages.read_page(schema.name, page_id)
            for slot_id, values in enumerate(page.slots):
                if values is not None:
                    yield Record(RecordId(page_id, slot_id), dict(values))

    def fetch(self, table: str, rid: RecordId) -> Record:
        schema = self.catalog.table(table)
        page = self.pages.read_page(schema.name, rid.page_id)
        values = self._slot(page, rid)
        return Record(rid, dict(values))

    def update(self, table: str, rid: RecordId, assignments: dict[str, Scalar]) -> None:
        schema = self.catalog.table(table)
        if not isinstance(assignments, dict) or not assignments:
            raise ValidationError("Update needs at least one assignment")
        normalized = {}
        for key, value in assignments.items():
            name = identifier(key)
            if name in normalized:
                raise ValidationError(f"Duplicate assignment: {name}")
            normalized[name] = value
        page = self.pages.read_page(schema.name, rid.page_id)
        values = schema.validate_record({**self._slot(page, rid), **normalized})
        self._check_primary_key(schema, values, exclude=rid)
        page.slots[rid.slot_id] = values
        self.pages.write_page(schema.name, page)

    def delete(self, table: str, rid: RecordId) -> None:
        schema = self.catalog.table(table)
        page = self.pages.read_page(schema.name, rid.page_id)
        self._slot(page, rid)
        page.slots[rid.slot_id] = None
        self.pages.write_page(schema.name, page)

    def _check_primary_key(
        self, schema: TableSchema, values: dict[str, Scalar], exclude: RecordId | None = None
    ) -> None:
        if schema.primary_key is None:
            return
        for record in self.scan(schema.name):
            if record.rid != exclude and record.values[schema.primary_key] == values[schema.primary_key]:
                raise AlreadyExistsError(f"Duplicate primary key: {values[schema.primary_key]!r}")

    @staticmethod
    def _slot(page: Page, rid: RecordId) -> dict[str, Scalar]:
        if rid.slot_id >= len(page.slots) or page.slots[rid.slot_id] is None:
            raise NotFoundError(f"Missing record: {rid}")
        return page.slots[rid.slot_id]
