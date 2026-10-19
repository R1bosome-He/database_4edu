from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from edudb.__main__ import main


class CLITest(unittest.TestCase):
    def test_contract_demo_runs_with_explicit_test_double_warning(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["contract-demo"]), 0)
        self.assertIn("NOT a persistent WAL implementation", output.getvalue())
        self.assertIn("Fixed handles after apply: 0", output.getvalue())
        self.assertIn("Objects after reopen:", output.getvalue())

    def test_buffer_demo_runs_and_never_resets_existing_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["buffer-demo", "--db", temporary]), 0)
            text = output.getvalue()
            self.assertIn("LRU: disk_reads=3, hits=1, evictions=1", text)
            self.assertIn("Warm reads: disk_reads=0, hits=2", text)
            self.assertIn("Dirty pages after flush: 0", text)
            self.assertIn("Rows after reopen: 4; updated value: updated in buffer", text)
            root = Path(temporary)
            originals = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(["buffer-demo", "--db", temporary])
            self.assertEqual(error.exception.code, 1)
            self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_migration_cli_requires_path_and_is_idempotent(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["migrate-storage"])
        self.assertEqual(error.exception.code, 2)
        with tempfile.TemporaryDirectory() as temporary:
            with redirect_stdout(io.StringIO()):
                main(["buffer-demo", "--db", temporary])
            (Path(temporary) / "storage.json").unlink()
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["migrate-storage", "--db", temporary]), 0)
                self.assertEqual(main(["migrate-storage", "--db", temporary]), 0)
            self.assertIn("upgraded to version 2", output.getvalue())
            self.assertIn("already version 2", output.getvalue())


if __name__ == "__main__":
    unittest.main()
