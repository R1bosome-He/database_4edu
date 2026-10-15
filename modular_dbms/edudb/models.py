from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .errors import ValidationError

Scalar = str | int | float | bool | None
MAX_PAGE_ID = 99_999_999


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValidationError(f"Invalid identifier: {value!r}")
    return value.lower()


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", identifier(self.name))
        if not isinstance(self.type, str) or self.type.upper() not in {"INT", "FLOAT", "TEXT", "BOOL"}:
            raise ValidationError(f"Unsupported type: {self.type!r}")
        object.__setattr__(self, "type", self.type.upper())
        if type(self.nullable) is not bool:
            raise ValidationError("nullable must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "nullable": self.nullable}


@dataclass(frozen=True)
class TableSchema:
    name: str
    columns: tuple[Column, ...]
    primary_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", identifier(self.name))
        object.__setattr__(self, "columns", tuple(self.columns))
        if not self.columns or any(not isinstance(c, Column) for c in self.columns):
            raise ValidationError("A table needs at least one Column")
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)):
            raise ValidationError("Column names must be unique")
        if self.primary_key is not None:
            object.__setattr__(self, "primary_key", identifier(self.primary_key))
            if self.primary_key not in names:
                raise ValidationError("Primary key must name an existing column")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "columns": [column.to_dict() for column in self.columns],
            "primary_key": self.primary_key,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TableSchema:
        if set(value) != {"name", "columns", "primary_key"} or not isinstance(value["columns"], list):
            raise ValidationError("Invalid table schema")
        return cls(value["name"], tuple(Column(**c) for c in value["columns"]), value["primary_key"])

    def validate_record(self, values: dict[str, Scalar]) -> dict[str, Scalar]:
        if not isinstance(values, dict):
            raise ValidationError("Record must be a dictionary")
        normalized: dict[str, Scalar] = {}
        for key, value in values.items():
            name = identifier(key)
            if name in normalized:
                raise ValidationError(f"Duplicate field: {name}")
            normalized[name] = value
        unknown = normalized.keys() - {c.name for c in self.columns}
        if unknown:
            raise ValidationError(f"Unknown columns: {', '.join(sorted(unknown))}")
        result: dict[str, Scalar] = {}
        for column in self.columns:
            value = normalized.get(column.name)
            if value is None:
                if not column.nullable or column.name == self.primary_key:
                    raise ValidationError(f"Column {column.name} cannot be NULL")
            elif column.type == "INT":
                if type(value) is not int or not -(2**63) <= value < 2**63:
                    raise ValidationError(f"Column {column.name} requires a signed 64-bit INT")
            elif column.type == "FLOAT":
                if type(value) not in (int, float):
                    raise ValidationError(f"Column {column.name} requires a finite FLOAT")
                try:
                    value = float(value)
                except (OverflowError, ValueError) as exc:
                    raise ValidationError(f"Column {column.name} requires a finite FLOAT") from exc
                if not math.isfinite(value):
                    raise ValidationError(f"Column {column.name} requires a finite FLOAT")
            elif column.type == "TEXT" and type(value) is not str:
                raise ValidationError(f"Column {column.name} requires TEXT")
            elif column.type == "BOOL" and type(value) is not bool:
                raise ValidationError(f"Column {column.name} requires BOOL")
            result[column.name] = value
        return result


@dataclass(frozen=True, order=True)
class RecordId:
    page_id: int
    slot_id: int

    def __post_init__(self) -> None:
        if type(self.page_id) is not int or not 0 <= self.page_id <= MAX_PAGE_ID:
            raise ValidationError("Invalid page id")
        if type(self.slot_id) is not int or self.slot_id < 0:
            raise ValidationError("Invalid slot id")

    def __str__(self) -> str:
        return f"{self.page_id}:{self.slot_id}"


@dataclass
class Page:
    page_id: int
    slots: list[dict[str, Scalar] | None]


@dataclass(frozen=True)
class Record:
    rid: RecordId
    values: dict[str, Scalar]
