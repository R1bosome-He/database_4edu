from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from .legacy_api import PAGE_SIZE, BufferPool, Catalog, Column, DiskManager, RecordStore, TableSchema
from .errors import DatabaseError
from .contracts import DBError
from .models import Page


def storage_demo(root: Path) -> None:
    catalog = Catalog(root)
    disk = DiskManager(root)
    records = RecordStore(catalog, disk)
    records.create_table(TableSchema("students", (Column("id", "INT"), Column("name", "TEXT")), "id"))
    first = records.insert("students", {"id": 1, "name": "student_1"})
    for number in range(2, 181):
        records.insert("students", {"id": number, "name": f"student_{number}"})
    records.update("students", first, {"name": "updated"})
    reopened = RecordStore(Catalog(root), DiskManager(root))
    rows = list(reopened.scan("students"))
    print(f"Database: {root}")
    print(f"Rows after reopen: {len(rows)}")
    print(f"Pages: {len(disk.page_ids('students'))}; page size: {PAGE_SIZE} bytes")
    print(f"RID {first}: {reopened.fetch('students', first).values}")
    reopened.delete("students", first)
    print(f"Rows after deleting RID {first}: {len(list(reopened.scan('students')))}")
    print("M1 storage demo complete. SQL and Web are later modules.")


def buffer_demo(root: Path, capacity: int = 2) -> None:
    disk = DiskManager(root)
    catalog = Catalog(root)
    with BufferPool(disk, capacity) as pool:
        records = RecordStore(catalog, pool)
        records.create_table(TableSchema("buffer_demo", (Column("id", "INT"), Column("name", "TEXT")), "id"))
        for page_id in range(3):
            disk.write_page("buffer_demo", Page(page_id, [{"id": page_id, "name": "original"}]))
        print(f"Database: {root}; buffer capacity: {capacity}")
        for page_id in (0, 1, 0, 2):
            pool.read_page("buffer_demo", page_id)
            print(f"Read {page_id}; LRU -> MRU: {[key[1] for key in pool.cached_keys]}")
        stats = pool.snapshot_metrics()
        print(f"LRU: disk_reads={stats['disk_reads']}, hits={stats['cache_hits']}, evictions={stats['evictions']}")
        pool.reset_metrics()
        pool.read_page("buffer_demo", 2)
        pool.read_page("buffer_demo", 2)
        stats = pool.snapshot_metrics()
        print(f"Warm reads: disk_reads={stats['disk_reads']}, hits={stats['cache_hits']}")
        page = pool.read_page("buffer_demo", 2)
        page.slots[0]["name"] = "updated in buffer"
        pool.write_page("buffer_demo", page)
        print(f"Before flush, disk value: {disk.read_page('buffer_demo', 2).slots[0]['name']}")
        pool.write_page("buffer_demo", Page(3, [{"id": 3, "name": "new buffered page"}]))
        print(f"Visible page ids: {pool.page_ids('buffer_demo')}")
        print(f"Dirty pages before flush: {pool.snapshot_metrics()['dirty_pages']}")
        pool.flush()
        print(f"Dirty pages after flush: {pool.snapshot_metrics()['dirty_pages']}")
        print(f"Write-back stats: {pool.snapshot_metrics()}")
    reopened = RecordStore(Catalog(root), DiskManager(root))
    rows = list(reopened.scan("buffer_demo"))
    print(f"Rows after reopen: {len(rows)}; updated value: {rows[2].values['name']}")
    print("M2 buffer demo complete. Counts exclude setup and direct disk inspection.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EduDB modular project: storage and buffer pool")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("contract-demo", help="Run A's contract with explicitly labelled in-memory C test doubles")
    for name in ("storage-demo", "buffer-demo"):
        command = commands.add_parser(name)
        command.add_argument("--db", type=Path, help="Use a new directory; existing tables are never reset")
        if name == "buffer-demo":
            command.add_argument("--buffer-pages", type=int, default=2)
    migration = commands.add_parser("migrate-storage", help="Add an allocation manifest to intact M1 data")
    migration.add_argument("--db", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "contract-demo":
            from .examples.contract_demo import run
            run()
        elif args.command == "migrate-storage":
            changed = DiskManager.migrate_legacy(args.db)
            print("Storage layout upgraded to version 2; page bytes unchanged." if changed else "Storage layout is already version 2.")
        else:
            def run_demo(root: Path) -> None:
                if args.command == "buffer-demo":
                    buffer_demo(root, args.buffer_pages)
                else:
                    storage_demo(root)

            if args.db is not None:
                run_demo(args.db)
            else:
                with tempfile.TemporaryDirectory(prefix="edudb-modular-") as temporary:
                    run_demo(Path(temporary))
                print("Temporary demo database removed.")
    except (DBError, DatabaseError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
