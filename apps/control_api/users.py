"""Operator credentials store for control-plane login.

File: {project_root}/.control_api_users.json (gitignored)
Schema:
  {
    "version": 1,
    "users": {
      "admin": {"password_hash": "scrypt$...", "disabled": false}
    }
  }
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from apps.control_api.passwords import hash_password, verify_password

USERS_FILENAME = ".control_api_users.json"
_lock = threading.Lock()


def users_path(project_root: Path) -> Path:
    return project_root / USERS_FILENAME


def _default_doc() -> dict[str, Any]:
    return {"version": 1, "users": {}}


def load_users_doc(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _default_doc()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _default_doc()
    if not isinstance(data, dict):
        return _default_doc()
    users = data.get("users")
    if not isinstance(users, dict):
        data = _default_doc()
    else:
        data.setdefault("version", 1)
        data["users"] = users
    return data


def save_users_doc(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def list_usernames(project_root: Path) -> list[str]:
    doc = load_users_doc(users_path(project_root))
    return sorted(str(u) for u in (doc.get("users") or {}) if str(u))


def has_any_user(project_root: Path) -> bool:
    return bool(list_usernames(project_root))


def authenticate(project_root: Path, username: str, password: str) -> bool:
    username = (username or "").strip()
    if not username or not password:
        return False
    path = users_path(project_root)
    with _lock:
        doc = load_users_doc(path)
        rec = (doc.get("users") or {}).get(username)
        if not isinstance(rec, dict):
            return False
        if rec.get("disabled"):
            return False
        hashed = rec.get("password_hash") or ""
        return verify_password(password, str(hashed))


def upsert_user(project_root: Path, username: str, password: str, *, disabled: bool = False) -> None:
    username = (username or "").strip()
    if not username:
        raise ValueError("username required")
    if len(username) > 64 or any(c in username for c in " \t\n\r/\\"):
        raise ValueError("invalid username")
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    path = users_path(project_root)
    with _lock:
        doc = load_users_doc(path)
        users = doc.setdefault("users", {})
        users[username] = {
            "password_hash": hash_password(password),
            "disabled": bool(disabled),
        }
        save_users_doc(path, doc)


def ensure_bootstrap_user(
    project_root: Path,
    *,
    username: str | None,
    password: str | None,
) -> str | None:
    """Create first user from env if store empty. Returns created username or None."""
    if has_any_user(project_root):
        return None
    if not username or not password:
        return None
    upsert_user(project_root, username, password, disabled=False)
    return username.strip()
