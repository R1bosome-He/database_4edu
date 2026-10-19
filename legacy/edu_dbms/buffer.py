from __future__ import annotations

from collections import OrderedDict

from .models import BufferMetrics, Page
from .storage import StorageManager


class BufferManager:
    """A tiny LRU buffer pool sitting between the executor and disk pages."""

    def __init__(self, storage: StorageManager, capacity: int = 16) -> None:
        if capacity <= 0:
            raise ValueError("Buffer capacity must be positive")
        self.storage = storage
        self.capacity = capacity
        self.cache: OrderedDict[tuple[str, int], Page] = OrderedDict()
        self.dirty: set[tuple[str, int]] = set()
        self.metrics = BufferMetrics()

    def get_page(self, table: str, page_id: int) -> Page:
        key = (table.lower(), page_id)
        self.metrics.pages_touched += 1
        if key in self.cache:
            self.metrics.cache_hits += 1
            self.cache.move_to_end(key)
            return self.cache[key]
        self.metrics.cache_misses += 1
        self.metrics.disk_reads += 1
        page = self.storage.read_page(*key)
        if len(self.cache) >= self.capacity:
            self._evict_one()
        self.cache[key] = page
        return page

    def mark_dirty(self, page: Page) -> None:
        self.dirty.add(page.key)
        self.cache[page.key] = page
        self.cache.move_to_end(page.key)

    def flush_all(self) -> None:
        for key, page in list(self.cache.items()):
            if key in self.dirty:
                self.storage.write_page(page)
                self.metrics.disk_writes += 1
                self.dirty.remove(key)

    def reset_metrics(self) -> None:
        self.metrics = BufferMetrics()

    def snapshot_metrics(self) -> dict[str, int]:
        return self.metrics.to_dict()

    def clear(self) -> None:
        self.flush_all()
        self.cache.clear()
        self.dirty.clear()

    def _evict_one(self) -> None:
        key, page = self.cache.popitem(last=False)
        self.metrics.evictions += 1
        if key in self.dirty:
            self.storage.write_page(page)
            self.metrics.disk_writes += 1
            self.dirty.remove(key)

