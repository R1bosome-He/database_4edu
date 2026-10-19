from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class User:
    username: str
    password_hash: str
    is_admin: bool = False
    permissions: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "username": self.username,
            "password_hash": self.password_hash,
            "is_admin": self.is_admin,
            "permissions": self.permissions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "User":
        return cls(
            username=str(data["username"]),
            password_hash=str(data["password_hash"]),
            is_admin=bool(data.get("is_admin", False)),
            permissions={
                table: list(actions)
                for table, actions in dict(data.get("permissions", {})).items()
            },
        )


class SecurityManager:
    """Simple user, password, and table-level RBAC manager."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "security.json"
        self.users: dict[str, User] = {}
        self.load()
        if "admin" not in self.users:
            self.users["admin"] = User("admin", _hash_password("admin"), is_admin=True)
            self.save()

    def load(self) -> None:
        if not self.path.exists():
            self.users = {}
            return
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.users = {
            username.lower(): User.from_dict(user)
            for username, user in data.get("users", {}).items()
        }

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(
                {"users": {name: user.to_dict() for name, user in self.users.items()}},
                fh,
                ensure_ascii=False,
                indent=2,
            )
        tmp.replace(self.path)

    def create_user(self, username: str, password: str) -> None:
        lowered = username.lower()
        if lowered in self.users:
            raise ValueError(f"User already exists: {username}")
        self.users[lowered] = User(lowered, _hash_password(password))
        self.save()

    def authenticate(self, username: str, password: str) -> bool:
        user = self.users.get(username.lower())
        return bool(user and user.password_hash == _hash_password(password))

    def grant(self, username: str, table: str, action: str) -> None:
        lowered = username.lower()
        if lowered not in self.users:
            raise KeyError(f"Unknown user: {username}")
        actions = ["SELECT", "INSERT", "UPDATE", "DELETE"] if action.upper() == "ALL" else [action.upper()]
        permissions = self.users[lowered].permissions.setdefault(table.lower(), [])
        for item in actions:
            if item not in permissions:
                permissions.append(item)
        self.save()

    def require_admin(self, username: str) -> None:
        user = self.users.get(username.lower())
        if not user or not user.is_admin:
            raise PermissionError("Admin permission required")

    def require(self, username: str, action: str, tables: list[str]) -> None:
        user = self.users.get(username.lower())
        if not user:
            raise PermissionError(f"Unknown user: {username}")
        if user.is_admin:
            return
        action = action.upper()
        for table in tables:
            allowed = user.permissions.get(table.lower(), [])
            if action not in allowed and "ALL" not in allowed:
                raise PermissionError(f"{username} lacks {action} on {table}")


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()
