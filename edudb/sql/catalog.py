"""Catalog abstraction used by B for name/field binding and index selection.

B does not read A's pages directly. It depends on this small read-only view so
the planner can be unit-tested with an in-memory catalog, and run end-to-end
through the C runtime with RuntimeCatalog.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..c_api import DBError, PageKey


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str              # INT | FLOAT | TEXT | BOOL
    nullable: bool = True


@dataclass(frozen=True)
class IndexDesc:
    id: str
    name: str
    column: str            # single-column indexes only, matching module C
    unique: bool = False


@dataclass
class TableInfo:
    name: str
    columns: list[ColumnInfo]
    primary_key: str | None = None
    indexes: list[IndexDesc] = field(default_factory=list)

    def column(self, name: str) -> ColumnInfo | None:
        for column in self.columns:
            if column.name == name:
                return column
        return None


@dataclass
class ViewInfo:
    id: str
    name: str
    query_sql: str
    columns: list[ColumnInfo]
    dependencies: list[str]


class CatalogView(Protocol):
    def get_table(self, name: str) -> TableInfo | None: ...


class InMemoryCatalog:
    """Plain catalog for planner unit tests; no storage or transactions."""

    def __init__(self) -> None:
        self._tables: dict[str, TableInfo] = {}
        self._views: dict[str, ViewInfo] = {}

    def get_view(self, name):
        return self._views.get(name)

    def add_table(self, info: TableInfo) -> TableInfo:
        self._tables[info.name] = info
        return info

    def add_index(self, table: str, index: IndexDesc) -> None:
        self._tables[table].indexes.append(index)

    def get_table(self, name: str) -> TableInfo | None:
        return self._tables.get(name)


class RuntimeCatalog:
    """Adapter over integration.runtime.Runtime so B binds against live schemas.

    Schemas come from A's catalog bridge; index descriptors are read from C's
    ci_catalog page. This is read-only metadata and takes no locks, so it is a
    planning aid only, not a substitute for D's authenticated request path.
    """

    INDEX_CATALOG = PageKey("ci_catalog", 0)

    def __init__(self, runtime) -> None:
        self.runtime = runtime

    def get_view(self, name):
        if hasattr(self.runtime, "storage"):
            from ..contracts import DBError as StorageError
            try:
                view = self.runtime.storage.get_view(name)
            except StorageError as error:
                if error.code == "OBJECT_NOT_FOUND":
                    return None
                raise DBError(error.code, error.message) from error
            return ViewInfo(view.id, view.name, view.query_ast["sql"],
                            [ColumnInfo(c.name,c.type,c.nullable) for c in view.output_columns], view.dependencies)
        getter = getattr(self.runtime.catalog, "get_view", None)
        return getter(name) if getter else None

    def get_table(self, name: str) -> TableInfo | None:
        try:
            schema = self.runtime.catalog.table(name)
        except DBError:
            return None
        columns = [ColumnInfo(c.name, c.type, c.nullable) for c in schema.columns]
        info = TableInfo(schema.name, columns, schema.primary_key)
        image = self.runtime.pages.read_image(self.INDEX_CATALOG)
        for value in (image["payload"].values() if image else []):
            if value["table_id"] == schema.name and len(value["column_ids"]) == 1:
                info.indexes.append(IndexDesc(value["id"], value["name"],
                                              value["column_ids"][0], value["unique"]))
        return info
