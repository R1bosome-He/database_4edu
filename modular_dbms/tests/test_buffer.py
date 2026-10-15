from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from edudb.legacy_api import PAGE_SIZE, BufferPool, DiskManager
from edudb.errors import BufferClosedError, CorruptionError, NotFoundError, PageFullError, ValidationError
from edudb.models import Page


class BufferPoolTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.disk = DiskManager(self.root)
        for page_id in range(3):
            self.disk.write_page("t", Page(page_id, [{"id": page_id, "name": "old"}]))
        self.pool = BufferPool(self.disk, capacity=2)
        self.addCleanup(self.pool.close)

    def test_lru_reference_sequence(self) -> None:
        for page_id in (0, 1, 0, 2):
            self.pool.read_page("t", page_id)
        metrics = self.pool.snapshot_metrics()
        self.assertEqual(self.pool.cached_keys, (("t", 0), ("t", 2)))
        self.assertEqual(metrics["logical_reads"], 4)
        self.assertEqual(metrics["disk_reads"], 3)
        self.assertEqual(metrics["cache_hits"], 1)
        self.assertEqual(metrics["cache_misses"], 3)
        self.assertEqual(metrics["evictions"], 1)
        self.assertEqual(metrics["hit_ratio"], 0.25)
        self.assertEqual(metrics["disk_writes"], 0)

    def test_dirty_lru_written_once_before_eviction(self) -> None:
        self.pool.read_page("t", 0)
        page = self.pool.read_page("t", 1)
        page.slots[0]["name"] = "new"
        self.pool.write_page("t", page)
        self.pool.read_page("t", 0)
        self.assertEqual(self.disk.read_page("t", 1).slots[0]["name"], "old")
        self.pool.read_page("t", 2)
        self.assertEqual(self.disk.read_page("t", 1).slots[0]["name"], "new")
        self.pool.flush()
        self.assertEqual(self.pool.snapshot_metrics()["disk_writes"], 1)

    def test_reads_and_writes_do_not_expose_cached_objects(self) -> None:
        source = self.pool.read_page("t", 0)
        source.slots[0]["name"] = "new"
        self.pool.write_page("T", source)
        source.slots[0]["name"] = "mutated after write"
        returned = self.pool.read_page("t", 0)
        returned.slots[0]["name"] = "mutated after read"
        returned.slots.append(None)
        self.pool.flush()
        self.assertEqual(self.disk.read_page("t", 0).slots, [{"id": 0, "name": "new"}])

    def test_oversized_write_cannot_mutate_or_evict(self) -> None:
        self.pool.read_page("t", 0)
        self.pool.write_page("t", Page(1, [{"name": "dirty"}]))
        before = self.pool.snapshot_metrics()
        keys = self.pool.cached_keys
        for page_id in (1, 2):
            with self.subTest(page_id=page_id), self.assertRaises(PageFullError):
                self.pool.write_page("t", Page(page_id, [{"name": "x" * PAGE_SIZE}]))
        self.assertEqual(self.pool.cached_keys, keys)
        self.assertEqual(self.pool.snapshot_metrics(), before)
        self.assertEqual(self.pool.read_page("t", 1).slots[0]["name"], "dirty")

    def test_new_pages_are_visible_before_flush_and_survive_close(self) -> None:
        self.pool.write_page("t", Page(3, [{"id": 3}]))
        self.pool.write_page("t", Page(4, [{"id": 4}]))
        self.assertEqual(self.disk.page_ids("t"), (0, 1, 2))
        self.assertEqual(self.pool.page_ids("T"), (0, 1, 2, 3, 4))
        self.assertEqual(self.pool.read_page("t", 4).slots, [{"id": 4}])
        self.pool.close()
        reopened = DiskManager(self.root)
        self.assertEqual(reopened.page_ids("t"), (0, 1, 2, 3, 4))
        self.assertEqual(reopened.read_page("t", 4).slots, [{"id": 4}])

    def test_new_page_eviction_flushes_allocation_dependencies_in_order(self) -> None:
        self.pool.write_page("new", Page(0, [{"id": 0}]))
        self.pool.write_page("new", Page(1, [{"id": 1}]))
        self.pool.read_page("new", 0)
        with patch.object(self.disk, "write_page", wraps=self.disk.write_page) as write:
            self.pool.write_page("new", Page(2, [{"id": 2}]))
        self.assertEqual([call.args[1].page_id for call in write.call_args_list], [0, 1])
        self.assertEqual(self.pool.cached_keys, (("new", 0), ("new", 2)))
        self.assertEqual(self.disk.page_ids("new"), (0, 1))
        self.assertEqual(self.pool.snapshot_metrics()["disk_writes"], 2)

    def test_dirty_eviction_failure_retains_data_and_can_retry(self) -> None:
        self.pool.write_page("t", Page(0, [{"name": "dirty"}]))
        self.pool.read_page("t", 1)
        before = self.pool.cached_keys
        with patch.object(self.disk, "write_page", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.pool.read_page("t", 2)
        self.assertEqual(self.pool.cached_keys, before)
        self.assertEqual(self.pool.dirty_keys, (("t", 0),))
        self.assertEqual(self.pool.snapshot_metrics()["evictions"], 0)
        self.assertEqual(self.pool.snapshot_metrics()["write_errors"], 1)
        self.assertEqual(self.pool.snapshot_metrics()["disk_reads"], 2)
        self.pool.read_page("t", 2)
        self.assertEqual(self.disk.read_page("t", 0).slots[0]["name"], "dirty")
        self.assertEqual(self.pool.snapshot_metrics()["evictions"], 1)

    def test_failed_write_admission_does_not_accept_new_value(self) -> None:
        self.pool.write_page("t", Page(0, [{"name": "dirty"}]))
        self.pool.read_page("t", 1)
        with patch.object(self.disk, "write_page", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.pool.write_page("t", Page(2, [{"name": "not accepted"}]))
        self.assertNotIn(("t", 2), self.pool.cached_keys)
        self.assertEqual(self.pool.snapshot_metrics()["logical_writes"], 1)
        self.assertEqual(self.disk.read_page("t", 2).slots[0]["name"], "old")

    def test_dependency_failure_preserves_unwritten_frames_for_retry(self) -> None:
        self.pool.write_page("new", Page(0, [{"id": 0}]))
        self.pool.write_page("new", Page(1, [{"id": 1}]))
        self.pool.read_page("new", 0)
        original = self.disk.write_page

        def fail_second(table: str, page: Page) -> None:
            if page.page_id == 1:
                raise OSError("second page failed")
            original(table, page)

        with patch.object(self.disk, "write_page", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.pool.write_page("new", Page(2, [{"id": 2}]))
        self.assertEqual(self.pool.cached_keys, (("new", 1), ("new", 0)))
        self.assertEqual(self.pool.dirty_keys, (("new", 1),))
        self.assertEqual(self.disk.page_ids("new"), (0,))
        self.pool.write_page("new", Page(2, [{"id": 2}]))
        self.pool.flush()
        self.assertEqual(self.disk.page_ids("new"), (0, 1, 2))

    def test_flush_partial_failure_preserves_remaining_dirty_pages(self) -> None:
        self.pool.write_page("t", Page(0, [{"name": "zero"}]))
        self.pool.write_page("t", Page(1, [{"name": "one"}]))
        order = self.pool.cached_keys
        original = self.disk.write_page

        def fail_one(table: str, page: Page) -> None:
            if page.page_id == 1:
                raise OSError("write failed")
            original(table, page)

        with patch.object(self.disk, "write_page", side_effect=fail_one):
            with self.assertRaises(OSError):
                self.pool.clear()
        self.assertEqual(self.pool.cached_keys, order)
        self.assertEqual(self.pool.dirty_keys, (("t", 1),))
        self.assertEqual(self.pool.snapshot_metrics()["disk_writes"], 1)
        with patch.object(self.disk, "write_page", wraps=original) as write:
            self.pool.flush()
        self.assertEqual([call.args[1].page_id for call in write.call_args_list], [1])
        self.assertEqual(self.pool.cached_keys, order)
        self.pool.flush()
        self.assertEqual(self.pool.snapshot_metrics()["disk_writes"], 2)

    def test_failed_close_keeps_pool_open_for_retry(self) -> None:
        self.pool.write_page("t", Page(0, [{"name": "dirty"}]))
        with patch.object(self.disk, "write_page", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.pool.close()
        self.assertFalse(self.pool.closed)
        self.assertEqual(self.pool.read_page("t", 0).slots[0]["name"], "dirty")
        self.pool.close()
        self.assertTrue(self.pool.closed)
        self.pool.close()
        self.assertEqual(self.disk.read_page("t", 0).slots[0]["name"], "dirty")

    def test_failed_read_does_not_evict_dirty_pages(self) -> None:
        self.pool.write_page("t", Page(0, [{"name": "dirty"}]))
        self.pool.read_page("t", 1)
        order = self.pool.cached_keys
        with self.assertRaises(NotFoundError):
            self.pool.read_page("t", 99)
        self.assertEqual(self.pool.cached_keys, order)
        self.assertEqual(self.pool.snapshot_metrics()["read_errors"], 1)
        self.assertEqual(self.pool.snapshot_metrics()["disk_writes"], 0)
        self.assertEqual(self.pool.snapshot_metrics()["evictions"], 0)

    def test_corrupt_page_is_not_cached(self) -> None:
        path = self.disk.page_path("t", 2)
        path.write_bytes(b"broken")
        with self.assertRaises(CorruptionError):
            self.pool.read_page("t", 2)
        self.assertEqual(self.pool.cached_keys, ())
        self.assertEqual(self.pool.snapshot_metrics()["disk_reads"], 0)
        self.assertEqual(self.pool.snapshot_metrics()["read_errors"], 1)

    def test_reset_metrics_preserves_dirty_data_and_recency(self) -> None:
        self.pool.read_page("t", 0)
        self.pool.write_page("t", Page(1, [{"name": "dirty"}]))
        keys = self.pool.cached_keys
        self.pool.reset_metrics()
        snapshot = self.pool.snapshot_metrics()
        self.assertEqual(snapshot["logical_reads"], 0)
        self.assertEqual(snapshot["disk_reads"], 0)
        self.assertEqual(snapshot["dirty_pages"], 1)
        self.assertEqual(snapshot["hit_ratio"], 0.0)
        snapshot["dirty_pages"] = 99
        self.assertEqual(self.pool.cached_keys, keys)
        self.assertEqual(self.pool.snapshot_metrics()["dirty_pages"], 1)
        self.pool.read_page("t", 1)
        self.assertEqual(self.pool.snapshot_metrics()["cache_hits"], 1)
        self.assertEqual(self.pool.snapshot_metrics()["disk_reads"], 0)

    def test_clear_flushes_then_makes_next_read_cold(self) -> None:
        self.pool.write_page("t", Page(0, [{"name": "dirty"}]))
        self.pool.clear()
        self.assertEqual(self.pool.cached_keys, ())
        self.assertEqual(self.pool.snapshot_metrics()["disk_writes"], 1)
        self.assertEqual(self.pool.read_page("t", 0).slots[0]["name"], "dirty")
        self.assertEqual(self.pool.snapshot_metrics()["disk_reads"], 1)

    def test_capacity_one_and_table_namespaces(self) -> None:
        with BufferPool(self.disk, capacity=1) as pool:
            pool.write_page("other", Page(0, [{"name": "other"}]))
            self.assertEqual(pool.read_page("t", 0).slots[0]["name"], "old")
            self.assertEqual(pool.read_page("other", 0).slots[0]["name"], "other")
            self.assertEqual(len(pool.cached_keys), 1)

    def test_context_exit_flushes_even_when_body_raises(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with BufferPool(self.disk) as pool:
                pool.write_page("t", Page(0, [{"name": "accepted"}]))
                raise RuntimeError("body failed")
        self.assertTrue(pool.closed)
        self.assertEqual(self.disk.read_page("t", 0).slots[0]["name"], "accepted")

    def test_closed_pool_rejects_operations(self) -> None:
        self.pool.close()
        operations = [lambda: self.pool.read_page("t", 0), lambda: self.pool.write_page("t", Page(0, [])),
                      lambda: self.pool.page_ids("t"), self.pool.flush, self.pool.clear,
                      self.pool.reset_metrics, self.pool.__enter__]
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(BufferClosedError):
                operation()
        self.assertEqual(self.pool.snapshot_metrics()["cached_pages"], 0)

    def test_invalid_capacity_and_addresses(self) -> None:
        for capacity in (0, -1, True, 1.5):
            with self.subTest(capacity=capacity), self.assertRaises(ValidationError):
                BufferPool(self.disk, capacity=capacity)
        for table, page_id in (("../t", 0), ("t", -1), ("t", True)):
            with self.subTest(address=(table, page_id)), self.assertRaises(ValidationError):
                self.pool.read_page(table, page_id)
        self.assertEqual(self.pool.snapshot_metrics()["logical_reads"], 0)
        with self.assertRaises(ValidationError):
            self.pool.write_page("t", Page(4, []))


if __name__ == "__main__":
    unittest.main()
