from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass
from types import TracebackType

from ..errors import BufferClosedError, CorruptionError, ValidationError
from ..models import Page, RecordId, identifier
from ..storage.disk import DiskManager
from ..storage.page import encode_page

PageKey = tuple[str, int]


@dataclass
class _Frame:
    page: Page
    dirty: bool = False


@dataclass
class _Counters:
    logical_reads: int = 0
    logical_writes: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    disk_reads: int = 0
    disk_writes: int = 0
    read_errors: int = 0
    write_errors: int = 0
    evictions: int = 0


def _copy_page(page: Page) -> Page:
    return Page(page.page_id, [None if row is None else dict(row) for row in page.slots])


class BufferPool:
    """Single-owner LRU cache implementing PageIO with write-back persistence."""

    def __init__(self, disk: DiskManager, capacity: int = 16) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValidationError("Buffer capacity must be a positive integer")
        self.disk = disk
        self._capacity = capacity
        self._frames: OrderedDict[PageKey, _Frame] = OrderedDict()
        self._counters = _Counters()
        self._closed = False

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def cached_keys(self) -> tuple[PageKey, ...]:
        """Resident page keys in least-to-most recently used order."""
        return tuple(self._frames)

    @property
    def dirty_keys(self) -> tuple[PageKey, ...]:
        return tuple(key for key, frame in self._frames.items() if frame.dirty)

    def read_page(self, table: str, page_id: int) -> Page:
        self._ensure_open()
        key = self._key(table, page_id)
        self._counters.logical_reads += 1
        if key in self._frames:
            self._counters.cache_hits += 1
            self._frames.move_to_end(key)
            return _copy_page(self._frames[key].page)
        self._counters.cache_misses += 1
        try:
            page = self.disk.read_page(*key)
        except Exception:
            self._counters.read_errors += 1
            raise
        self._counters.disk_reads += 1
        # A failed read must not evict or write an unrelated resident page.
        self._make_room()
        self._frames[key] = _Frame(_copy_page(page))
        return _copy_page(page)

    def write_page(self, table: str, page: Page) -> None:
        self._ensure_open()
        key = self._key(table, page.page_id)
        encode_page(page)
        ids = self.page_ids(key[0])
        if page.page_id > len(ids):
            raise ValidationError("New pages must be appended in page-id order")
        copy = _copy_page(page)
        if key not in self._frames:
            self._make_room()
        self._frames[key] = _Frame(copy, dirty=True)
        self._frames.move_to_end(key)
        self._counters.logical_writes += 1

    def page_ids(self, table: str) -> tuple[int, ...]:
        self._ensure_open()
        table = identifier(table)
        ids = set(self.disk.page_ids(table))
        ids.update(page_id for name, page_id in self._frames if name == table)
        ordered = tuple(sorted(ids))
        if any(page_id != index for index, page_id in enumerate(ordered)):
            raise CorruptionError(f"Buffered page allocation contains a gap: {table}")
        return ordered

    def flush(self) -> None:
        self._ensure_open()
        for key in sorted(self._frames):
            if self._frames[key].dirty:
                self._flush_key(key)

    def clear(self) -> None:
        self.flush()
        self._frames.clear()

    def close(self) -> None:
        if not self._closed:
            self.clear()
            self._closed = True

    def reset_metrics(self) -> None:
        self._ensure_open()
        self._counters = _Counters()

    def snapshot_metrics(self) -> dict[str, int | float]:
        return {
            **asdict(self._counters),
            "hit_ratio": self._counters.cache_hits / self._counters.logical_reads
            if self._counters.logical_reads else 0.0,
            "cached_pages": len(self._frames),
            "dirty_pages": len(self.dirty_keys),
        }

    def _make_room(self) -> None:
        if len(self._frames) < self.capacity:
            return
        key = next(iter(self._frames))
        if self._frames[key].dirty:
            self._flush_key(key)
        del self._frames[key]
        self._counters.evictions += 1

    def _flush_key(self, key: PageKey) -> None:
        table, page_id = key
        persisted_count = len(self.disk.page_ids(table))
        # Newer dirty pages can become LRU before their unpersisted predecessors.
        pending = [(table, number) for number in range(persisted_count, page_id)]
        for dependency in pending:
            if dependency not in self._frames or not self._frames[dependency].dirty:
                raise CorruptionError(f"Missing pending allocation: {dependency}")
        for target in [*pending, key]:
            frame = self._frames[target]
            if not frame.dirty:
                continue
            try:
                self.disk.write_page(target[0], _copy_page(frame.page))
            except Exception:
                self._counters.write_errors += 1
                raise
            frame.dirty = False
            self._counters.disk_writes += 1

    def _ensure_open(self) -> None:
        if self._closed:
            raise BufferClosedError("Buffer pool is closed")

    @staticmethod
    def _key(table: str, page_id: int) -> PageKey:
        RecordId(page_id, 0)
        return identifier(table), page_id

    def __enter__(self) -> BufferPool:
        self._ensure_open()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
