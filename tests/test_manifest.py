from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from edudb.legacy_api import BufferPool, Catalog, Column, DiskManager, TableSchema
from edudb.errors import CorruptionError, LegacyFormatError, NotFoundError, ValidationError
from edudb.models import Page
from edudb.storage.page import encode_page


class ManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.disk = DiskManager(self.root)
        Catalog(self.root).create_table(TableSchema("t", (Column("id", "INT"),), "id"))

    def legacy_pages(self) -> dict[Path, bytes]:
        self.disk.manifest_path.unlink()
        pages = {}
        for page_id in range(2):
            path = self.disk.page_path("t", page_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            pages[path] = encode_page(Page(page_id, [{"id": page_id}]))
            path.write_bytes(pages[path])
        return pages

    def test_deleted_tail_is_detected_after_reopen(self) -> None:
        for page_id in range(2):
            self.disk.write_page("t", Page(page_id, [{"id": page_id}]))
        self.disk.page_path("t", 1).unlink()
        reopened = DiskManager(self.root)
        with self.assertRaises(CorruptionError):
            reopened.page_ids("t")
        with self.assertRaises(CorruptionError):
            reopened.read_page("t", 1)
        with self.assertRaises(CorruptionError):
            reopened.write_page("t", Page(1, [{"id": 99}]))

    def test_deleted_table_directory_is_not_an_empty_table(self) -> None:
        self.disk.write_page("t", Page(0, [{"id": 0}]))
        path = self.disk.page_path("t", 0)
        path.unlink()
        path.parent.rmdir()
        with self.assertRaises(CorruptionError):
            DiskManager(self.root).page_ids("t")

    def test_unregistered_or_misnamed_file_is_rejected(self) -> None:
        self.disk.write_page("t", Page(0, []))
        for name in ("00000001.page", "bad.page"):
            path = self.disk.page_path("t", 0).with_name(name)
            path.write_bytes(encode_page(Page(1, [])))
            with self.subTest(name=name), self.assertRaises(CorruptionError):
                self.disk.page_ids("t")
            path.unlink()

    def test_skipping_allocation_does_not_create_a_file(self) -> None:
        with self.assertRaises(ValidationError):
            self.disk.write_page("t", Page(2, []))
        self.assertEqual(self.disk.page_ids("t"), ())

    def test_failed_manifest_save_undoes_only_unregistered_append(self) -> None:
        self.disk.write_page("t", Page(0, [{"id": 0}]))
        original = self.disk.page_path("t", 0).read_bytes()
        manifest = self.disk.manifest_path.read_bytes()
        with patch.object(self.disk, "_save_counts", side_effect=OSError("manifest full")):
            with self.assertRaises(OSError):
                self.disk.write_page("t", Page(1, [{"id": 1}]))
        self.assertEqual(self.disk.page_path("t", 0).read_bytes(), original)
        self.assertEqual(self.disk.manifest_path.read_bytes(), manifest)
        self.assertFalse(self.disk.page_path("t", 1).exists())
        self.disk.write_page("t", Page(1, [{"id": 1}]))
        self.assertEqual(self.disk.page_ids("t"), (0, 1))

    def test_buffer_keeps_append_dirty_when_manifest_save_fails(self) -> None:
        with BufferPool(self.disk, capacity=1) as pool:
            pool.write_page("t", Page(0, [{"id": 0}]))
            with patch.object(self.disk, "_save_counts", side_effect=OSError("manifest full")):
                with self.assertRaises(OSError):
                    pool.flush()
            self.assertEqual(pool.dirty_keys, (("t", 0),))
            self.assertEqual(self.disk.page_ids("t"), ())
            self.assertEqual(pool.snapshot_metrics()["disk_writes"], 0)
            self.assertEqual(pool.snapshot_metrics()["write_errors"], 1)
            pool.flush()
            self.assertEqual(self.disk.read_page("t", 0).slots, [{"id": 0}])

    def test_bad_manifest_is_never_implicitly_rebuilt(self) -> None:
        valid = self.disk.manifest_path.read_bytes()
        malformed = [b"not json", b"[]", b"null", b'{}']
        for counts in ({"t": -1}, {"T": 1}, {"t": True}, {"../t": 0}):
            malformed.append(json.dumps({"format": "edudb.storage", "version": 2, "page_counts": counts}).encode())
        malformed.append(b'{"format":"edudb.storage","version":99,"page_counts":{}}')
        for raw in malformed:
            self.disk.manifest_path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(CorruptionError):
                DiskManager(self.root)
            self.assertEqual(self.disk.manifest_path.read_bytes(), raw)
        self.disk.manifest_path.write_bytes(valid)

    def test_missing_manifest_in_active_manager_is_an_error(self) -> None:
        self.disk.manifest_path.unlink()
        with self.assertRaises(CorruptionError):
            self.disk.page_ids("t")

    def test_legacy_data_requires_explicit_upgrade_without_page_rewrites(self) -> None:
        originals = self.legacy_pages()
        with self.assertRaises(LegacyFormatError):
            DiskManager(self.root)
        self.assertFalse(self.disk.manifest_path.exists())
        self.assertTrue(DiskManager.migrate_legacy(self.root))
        reopened = DiskManager(self.root)
        self.assertEqual(reopened.page_ids("t"), (0, 1))
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data)
        before = reopened.manifest_path.read_bytes()
        self.assertFalse(DiskManager.migrate_legacy(self.root))
        self.assertEqual(reopened.manifest_path.read_bytes(), before)

    def test_upgrade_rejects_corruption_without_creating_manifest(self) -> None:
        originals = self.legacy_pages()
        path = next(iter(originals))
        path.write_bytes(b"corrupt")
        with self.assertRaises(CorruptionError):
            DiskManager.migrate_legacy(self.root)
        self.assertFalse(self.disk.manifest_path.exists())
        self.assertEqual(path.read_bytes(), b"corrupt")

    def test_upgrade_rejects_page_gap(self) -> None:
        originals = self.legacy_pages()
        next(iter(originals)).unlink()
        with self.assertRaises(CorruptionError):
            DiskManager.migrate_legacy(self.root)
        self.assertFalse(self.disk.manifest_path.exists())

    def test_upgrade_rejects_unknown_data_and_missing_catalog(self) -> None:
        self.legacy_pages()
        extra = self.root / "data" / "unknown"
        extra.mkdir()
        with self.assertRaises(CorruptionError):
            DiskManager.migrate_legacy(self.root)
        extra.rmdir()
        (self.root / "catalog.json").unlink()
        with self.assertRaises(NotFoundError):
            DiskManager.migrate_legacy(self.root)
        self.assertFalse(self.disk.manifest_path.exists())

    def test_upgrade_write_failure_preserves_legacy_pages(self) -> None:
        originals = self.legacy_pages()
        with patch("edudb.storage.disk.atomic_write", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                DiskManager.migrate_legacy(self.root)
        self.assertFalse(self.disk.manifest_path.exists())
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
