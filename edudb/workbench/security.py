from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from pathlib import Path

from ..c_api import DBError
from ..fileio import atomic_write

ITERATIONS = 600_000
ACTIONS = {"SELECT", "INSERT", "UPDATE", "DELETE"}


class IdentityStore:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text("utf-8")) if path.exists() else {"users": {}}

    def _save(self, data):
        atomic_write(self.path, json.dumps(data, ensure_ascii=False).encode("utf-8"))
        self.data = data

    def create(self, username, password, role="member"):
        if not isinstance(username, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,31}", username):
            raise DBError("INVALID_USER", "Username: 3-32 lowercase letters, digits or underscores")
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise DBError("WEAK_PASSWORD", "Password must contain 12-128 characters")
        if role not in {"admin", "member"}:
            raise DBError("INVALID_ROLE", "Unknown role")
        if username in self.data["users"]:
            raise DBError("USER_EXISTS", "User already exists")
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
        data = json.loads(json.dumps(self.data))
        data["users"][username] = dict(salt=salt, digest=digest, iterations=ITERATIONS, role=role, grants={})
        self._save(data)

    def verify(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or len(password) > 128:
            return False
        user = self.data["users"].get(username)
        salt = user["salt"] if user else "00" * 16
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
        return hmac.compare_digest(digest, user["digest"] if user else "00" * 32) and user is not None

    def public(self, username):
        user = self.data["users"][username]
        return dict(username=username, role=user["role"], grants=user["grants"])

    def admin(self, username):
        return self.data["users"][username]["role"] == "admin"

    def allowed(self, username, table_id, action):
        return self.admin(username) or action in self.data["users"][username]["grants"].get(table_id, [])

    def grant(self, username, table_id, actions):
        if username not in self.data["users"] or not isinstance(actions, list) or not set(actions) <= ACTIONS:
            raise DBError("INVALID_GRANT", "Unknown user or privilege")
        data = json.loads(json.dumps(self.data))
        data["users"][username]["grants"][table_id] = sorted(set(actions))
        self._save(data)
