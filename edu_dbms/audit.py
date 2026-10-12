from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLogger:
    def __init__(self, root: Path) -> None:
        self.path = root / "audit.log"

    def log(
        self,
        *,
        user: str,
        sql: str,
        success: bool,
        message: str,
        elapsed_ms: float,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "user": user,
            "sql": sql.strip(),
            "success": success,
            "message": message,
            "elapsed_ms": round(elapsed_ms, 3),
            "metrics": metrics or {},
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

