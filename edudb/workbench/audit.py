from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ..c_api import DBError
from ..sql.tokens import tokenize


def fingerprint(sql):
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def redact(sql):
    try:
        return " ".join("?" if token.type in {"STRING", "INT", "FLOAT"} else token.text
                        for token in tokenize(sql) if token.type != "EOF")[:1000]
    except DBError:
        return "[unparsed statement]"


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.events = []
        self.tail = "0" * 64
        if path.exists():
            for line in path.read_text("utf-8").splitlines():
                event = json.loads(line)
                digest = event.pop("hash")
                if event["previous"] != self.tail or self._hash(event) != digest:
                    raise DBError("AUDIT_CORRUPTION", "Audit chain verification failed")
                self.tail = digest
                self.events.append({**event, "hash": digest})

    @staticmethod
    def _hash(event):
        return hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def append(self, action, user=None, success=True, **details):
        event = dict(sequence=len(self.events) + 1, time=datetime.now(timezone.utc).isoformat(),
                     action=action, user=user, success=success, previous=self.tail, **details)
        digest = self._hash(event)
        event["hash"] = digest
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.tail = digest
        self.events.append(event)
        return event
