from __future__ import annotations

import json
from pathlib import Path

from .errors import AlreadyExistsError, CorruptionError, NotFoundError, ValidationError
from .fileio import atomic_write
from .models import TableSchema, identifier


class Catalog:
    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / "catalog.json"
        self._tables: dict[str, TableSchema] = {}
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data["format"] != "edudb.catalog" or type(data["version"]) is not int or data["version"] != 1:
                raise ValidationError("Unsupported catalog format")
            if not isinstance(data["tables"], dict):
                raise ValidationError("Invalid table map")
            tables = {name: TableSchema.from_dict(schema) for name, schema in data["tables"].items()}
            if any(name != schema.name for name, schema in tables.items()):
                raise ValidationError("Catalog table name mismatch")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise CorruptionError(f"Invalid catalog: {self.path}") from exc
        self._tables = tables

    def create_table(self, schema: TableSchema) -> None:
        if schema.name in self._tables:
            raise AlreadyExistsError(f"Table already exists: {schema.name}")
        updated = {**self._tables, schema.name: schema}
        data = {"format": "edudb.catalog", "version": 1, "tables": {n: s.to_dict() for n, s in updated.items()}}
        atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
        self._tables = updated

    def table(self, name: str) -> TableSchema:
        name = identifier(name)
        if name not in self._tables:
            raise NotFoundError(f"Unknown table: {name}")
        return self._tables[name]

    def tables(self) -> tuple[TableSchema, ...]:
        return tuple(self._tables[name] for name in sorted(self._tables))
