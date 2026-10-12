from __future__ import annotations

import json
from pathlib import Path

from .models import Column, IndexMeta, TableSchema


class Catalog:
    """Persistent metadata for relations, columns, pages, and indexes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "catalog.json"
        self.tables: dict[str, TableSchema] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.tables = {}
            return
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.tables = {
            name.lower(): TableSchema.from_dict(schema)
            for name, schema in data.get("tables", {}).items()
        }

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = {"tables": {name: schema.to_dict() for name, schema in self.tables.items()}}
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def create_table(
        self, name: str, columns: list[Column], primary_key: str | None = None
    ) -> TableSchema:
        lowered = name.lower()
        if lowered in self.tables:
            raise ValueError(f"Table already exists: {name}")
        schema = TableSchema(lowered, columns, primary_key)
        self.tables[lowered] = schema
        self.save()
        return schema

    def table(self, name: str) -> TableSchema:
        lowered = name.lower()
        if lowered not in self.tables:
            raise KeyError(f"Unknown table: {name}")
        return self.tables[lowered]

    def add_index(self, table: str, index_name: str, column: str) -> None:
        schema = self.table(table)
        schema.column(column)
        lowered_name = index_name.lower()
        lowered_column = column.lower()
        if any(index.name == lowered_name for index in schema.indexes):
            raise ValueError(f"Index already exists: {index_name}")
        if any(index.column == lowered_column for index in schema.indexes):
            raise ValueError(f"Column already has an index: {table}.{column}")
        schema.indexes.append(IndexMeta(lowered_name, lowered_column))
        self.save()

    def update_table(self, schema: TableSchema) -> None:
        self.tables[schema.name] = schema
        self.save()

