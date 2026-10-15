from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from edudb import Catalog, Column, TableSchema
from edudb.errors import AlreadyExistsError, CorruptionError, NotFoundError, ValidationError


class CatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.catalog = Catalog(self.root)
        self.schema = TableSchema("Students", (Column("ID", "int"), Column("name", "TEXT")), "ID")

    def test_schema_survives_reopen(self) -> None:
        self.catalog.create_table(self.schema)
        reopened = Catalog(self.root)
        self.assertEqual(reopened.table("STUDENTS"), self.schema)
        self.assertEqual(reopened.tables(), (self.schema,))

    def test_duplicate_table_does_not_overwrite_metadata(self) -> None:
        self.catalog.create_table(self.schema)
        before = self.catalog.path.read_bytes()
        with self.assertRaises(AlreadyExistsError):
            self.catalog.create_table(TableSchema("students", (Column("other", "BOOL"),)))
        self.assertEqual(self.catalog.path.read_bytes(), before)
        self.assertEqual(self.catalog.table("students"), self.schema)

    def test_unknown_table_is_an_error(self) -> None:
        with self.assertRaises(NotFoundError):
            self.catalog.table("missing")

    def test_invalid_metadata_is_not_silently_replaced(self) -> None:
        invalid = b'{"format":"edudb.catalog","version":99,"tables":{}}'
        self.catalog.path.write_bytes(invalid)
        with self.assertRaises(CorruptionError):
            Catalog(self.root)
        self.assertEqual(self.catalog.path.read_bytes(), invalid)

    def test_failed_save_does_not_publish_schema(self) -> None:
        with patch("edudb.catalog.atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.catalog.create_table(self.schema)
        self.assertEqual(self.catalog.tables(), ())
        self.assertFalse(self.catalog.path.exists())

    def test_schema_rejects_invalid_names_types_and_keys(self) -> None:
        factories = [
            lambda: TableSchema("../outside", (Column("id", "INT"),)),
            lambda: TableSchema("t", (Column("id", "INT"), Column("ID", "INT"))),
            lambda: TableSchema("t", (Column("id", "INT"),), "missing"),
            lambda: TableSchema("t", ()),
            lambda: Column("x", "DATE"),
            lambda: Column("x", "TEXT", nullable="yes"),
        ]
        for factory in factories:
            with self.subTest(factory=factories.index(factory)), self.assertRaises(ValidationError):
                factory()

    def test_scalar_types_and_nullable_rules(self) -> None:
        schema = TableSchema("t", (
            Column("id", "INT"), Column("text", "TEXT"),
            Column("value", "FLOAT"), Column("flag", "BOOL", nullable=False),
        ), "id")
        self.assertEqual(schema.validate_record({"id": 1, "value": 2, "flag": True}),
                         {"id": 1, "text": None, "value": 2.0, "flag": True})
        invalid = [
            {"id": True, "flag": True}, {"id": 1.2, "flag": True},
            {"id": 2**63, "flag": True}, {"id": None, "flag": True},
            {"id": 1}, {"id": 1, "flag": 1},
            {"id": 1, "flag": True, "value": float("nan")},
            {"id": 1, "flag": True, "value": 10**400},
            {"id": 1, "flag": True, "extra": "x"},
            {"id": 1, "flag": True, "text": 42},
            {"id": 1, "ID": 2, "flag": True},
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                schema.validate_record(values)


if __name__ == "__main__":
    unittest.main()
