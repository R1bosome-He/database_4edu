"""Run real unittest discovery and persist source-bound evidence for reports."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import platform
import time
import unittest
from pathlib import Path

from ..fileio import atomic_write

ROOT = Path(__file__).resolve().parents[2]


def source_hash(root=ROOT):
    digest = hashlib.sha256()
    paths = sorted([*root.glob("edudb/**/*.py"), *root.glob("tests/**/*.py"),
                    *root.glob("edudb/workbench/static/*")])
    for path in paths:
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def load_evidence(path):
    if not path or not Path(path).exists():
        return {"status": "not_run", "note": "No test evidence supplied to this server"}
    try:
        evidence = json.loads(Path(path).read_text("utf-8"))
        if evidence.get("source_sha256") != source_hash():
            return {"status": "stale", "note": "Source changed after the recorded test run"}
        return evidence
    except (OSError, ValueError):
        return {"status": "unavailable", "note": "Test evidence could not be read"}


def main():
    parser = argparse.ArgumentParser(description="Run tests and record source-bound report evidence")
    parser.add_argument("--output", type=Path, default=Path(".test_tmp/workbench/test-evidence.json"))
    args = parser.parse_args()
    before = source_hash()
    stream = io.StringIO()
    started = time.perf_counter()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
    after = source_hash()
    evidence = dict(status="passed" if result.wasSuccessful() and before == after else "failed",
                    source_sha256=before, source_unchanged=before == after, tests_run=result.testsRun,
                    failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
                    duration_seconds=round(time.perf_counter()-started, 3), timestamp=time.time(),
                    python=platform.python_version(), platform=platform.platform(),
                    command="python -m unittest discover -s tests", output=stream.getvalue())
    atomic_write(args.output, json.dumps(evidence, ensure_ascii=False, indent=2).encode())
    print(stream.getvalue())
    print(f"Evidence: {args.output.resolve()}")
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
