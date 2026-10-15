from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from edudb import PAGE_SIZE, Catalog, Column, DiskManager, RecordId, RecordStore, TableSchema
from edudb.errors import AlreadyExistsError, CorruptionError, NotFoundError, PageFullError, ValidationError
from edudb.models import Page


class StorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.disk = DiskManager(self.root)
        self.store = RecordStore(Catalog(self.root), self.disk)
        self.store.create_table(TableSchema("t", (Column("id", "INT"), Column("name", "TEXT")), "id"))

    def test_fixed_page_size_and_utf8_round_trip(self) -> None:
        page = Page(0, [{"id": 1, "name": "\u6570\u636e\u5e93"}, None])
        self.disk.write_page("t", page)
        self.assertEqual(self.disk.page_path("t", 0).stat().st_size, PAGE_SIZE)
        self.assertEqual(self.disk.read_page("t", 0), page)

    def test_page_reads_return_detached_values(self) -> None:
        rid = self.store.insert("t", {"id": 1, "name": "original"})
        page = self.disk.read_page("t", rid.page_id)
        page.slots[0]["name"] = "changed without write"
        self.assertEqual(self.store.fetch("t", rid).values["name"], "original")

    def test_multiple_pages_survive_reopen(self) -> None:
        expected = [{"id": number, "name": "x" * 180} for number in range(60)]
        rids = [self.store.insert("t", value) for value in expected]
        self.assertGreater(len(self.disk.page_ids("t")), 1)
        reopened = RecordStore(Catalog(self.root), DiskManager(self.root))
        self.assertEqual([r.values for r in reopened.scan("t")], expected)
        self.assertEqual([r.rid for r in reopened.scan("t")], rids)
        for page_id in self.disk.page_ids("t"):
            self.assertEqual(self.disk.page_path("t", page_id).stat().st_size, PAGE_SIZE)

    def test_duplicate_insert_changes_nothing(self) -> None:
        rid = self.store.insert("t", {"id": 1, "name": "original"})
        before = self.disk.page_path("t", 0).read_bytes()
        with self.assertRaises(AlreadyExistsError):
            self.store.insert("t", {"id": 1, "name": "duplicate"})
        self.assertEqual(self.disk.page_path("t", 0).read_bytes(), before)
        self.assertEqual(self.store.fetch("t", rid).values["name"], "original")

    def test_update_and_delete_keep_other_record_ids(self) -> None:
        first = self.store.insert("t", {"id": 1, "name": "first"})
        second = self.store.insert("t", {"id": 2, "name": "second"})
        self.store.update("t", second, {"NAME": "updated"})
        self.store.delete("t", first)
        with self.assertRaises(NotFoundError):
            self.store.fetch("t", first)
        third = self.store.insert("t", {"id": 3, "name": "third"})
        self.assertNotEqual(first, third)
        reopened = RecordStore(Catalog(self.root), DiskManager(self.root))
        self.assertEqual(reopened.fetch("t", second).values, {"id": 2, "name": "updated"})
        self.assertEqual([row.rid for row in reopened.scan("t")], [second, third])

    def test_failed_update_leaves_original_record(self) -> None:
        first = self.store.insert("t", {"id": 1, "name": "first"})
        self.store.insert("t", {"id": 2, "name": "second"})
        before = self.disk.page_path("t", 0).read_bytes()
        cases = [({"id": 2}, AlreadyExistsError), ({"id": None}, ValidationError),
                 ({"missing": 1}, ValidationError), ({"name": "x" * PAGE_SIZE}, PageFullError)]
        for assignments, error in cases:
            with self.subTest(assignments=tuple(assignments)), self.assertRaises(error):
                self.store.update("t", first, assignments)
            self.assertEqual(self.disk.page_path("t", 0).read_bytes(), before)
        self.store.update("t", first, {"id": 3})
        self.assertEqual(self.store.fetch("t", first).values["id"], 3)

    def test_oversized_insert_creates_no_page(self) -> None:
        with self.assertRaises(PageFullError):
            self.store.insert("t", {"id": 1, "name": "x" * PAGE_SIZE})
        self.assertEqual(self.disk.page_ids("t"), ())
        self.assertEqual(list(self.store.scan("t")), [])

    def test_type_error_creates_no_page(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.insert("t", {"id": "1", "name": "wrong type"})
        self.assertEqual(self.disk.page_ids("t"), ())

    def test_missing_page_or_record_is_an_error(self) -> None:
        with self.assertRaises(NotFoundError):
            self.disk.read_page("t", 0)
        rid = self.store.insert("t", {"id": 1, "name": "one"})
        for operation in (self.store.fetch, self.store.delete):
            with self.subTest(operation=operation.__name__), self.assertRaises(NotFoundError):
                operation("t", RecordId(rid.page_id, 99))
        self.assertEqual(len(list(self.store.scan("t"))), 1)

    def test_corrupt_pages_fail_explicitly(self) -> None:
        self.store.insert("t", {"id": 1, "name": "one"})
        path = self.disk.page_path("t", 0)
        original = path.read_bytes()
        variants = [original[:-1]]
        for position in (0, 4, 5, 11, 20, PAGE_SIZE - 1):
            modified = bytearray(original)
            modified[position] ^= 1
            variants.append(bytes(modified))
        for number, raw in enumerate(variants):
            path.write_bytes(raw)
            with self.subTest(variant=number), self.assertRaises(CorruptionError):
                self.disk.read_page("t", 0)

    def test_page_gap_is_not_silently_skipped(self) -> None:
        self.disk.write_page("t", Page(0, []))
        self.disk.write_page("t", Page(2, []))
        with self.assertRaises(CorruptionError):
            list(self.store.scan("t"))

    def test_failed_file_replace_preserves_page_and_removes_temporary_file(self) -> None:
        rid = self.store.insert("t", {"id": 1, "name": "original"})
        path = self.disk.page_path("t", 0)
        before = path.read_bytes()
        with patch("edudb.fileio.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.store.update("t", rid, {"name": "new"})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob(".pending-*")), [])

    def test_table_names_and_record_ids_cannot_escape_storage(self) -> None:
        for name in ("../other", "a/b", "a\\b", "C:\\data", ""):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.disk.write_page(name, Page(0, []))
        for page_id, slot_id in ((-1, 0), (0, -1), (True, 0), (0, False)):
            with self.subTest(rid=(page_id, slot_id)), self.assertRaises(ValidationError):
                RecordId(page_id, slot_id)

    def test_tables_have_independent_records(self) -> None:
        self.store.create_table(TableSchema("other", (Column("id", "INT"),), "id"))
        left = self.store.insert("t", {"id": 1, "name": "one"})
        right = self.store.insert("other", {"id": 1})
        self.store.delete("t", left)
        self.assertEqual(self.store.fetch("other", right).values, {"id": 1})


if __name__ == "__main__":
    unittest.main()
