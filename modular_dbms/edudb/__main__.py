from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from . import PAGE_SIZE, Catalog, Column, DiskManager, RecordStore, TableSchema
from .errors import DatabaseError


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EduDB modular project: M1 storage")
    parser.add_argument("command", choices=["storage-demo"])
    parser.add_argument("--db", type=Path, help="Use a new database directory; existing tables are never reset")
    args = parser.parse_args(argv)
    try:
        if args.db is not None:
            storage_demo(args.db)
        else:
            with tempfile.TemporaryDirectory(prefix="edudb-modular-") as temporary:
                storage_demo(Path(temporary))
            print("Temporary demo database removed.")
    except (DatabaseError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
