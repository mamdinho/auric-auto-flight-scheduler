"""
auth.py — Minimal username/password auth for the Auric Air Scheduler.

No external dependencies: passwords are hashed with PBKDF2-HMAC-SHA256
(stdlib hashlib) using a random per-user salt and 260,000 iterations (the
OWASP 2023 minimum for this algorithm). Plaintext passwords are never stored,
logged, or returned from any function here.

Users are stored in data/users.json (gitignored — never commit this file).
"""
from __future__ import annotations
import hashlib
import json
import os
import secrets
from datetime import datetime

import storage_paths

USERS_PATH = os.path.join(storage_paths.DATA_DIR, "users.json")

PBKDF2_ITERATIONS = 260_000
ROLES = ("admin", "ops")


def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    ).hex()


def hash_password(password: str) -> tuple[str, str]:
    """Returns (password_hash_hex, salt_hex) for a NEW password."""
    salt = secrets.token_bytes(16)
    return _hash(password, salt), salt.hex()


def verify_password(password: str, password_hash: str, salt_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    # constant-time comparison to avoid timing side-channels
    return secrets.compare_digest(_hash(password, salt), password_hash)


def load_users() -> dict:
    if not os.path.exists(USERS_PATH):
        return {}
    with open(USERS_PATH) as f:
        return json.load(f)


def save_users(users: dict) -> None:
    os.makedirs(os.path.dirname(USERS_PATH), exist_ok=True)
    with open(USERS_PATH, "w") as f:
        json.dump(users, f, indent=2)


def _normalize(username: str) -> str:
    return username.strip().lower()


def user_exists(username: str) -> bool:
    return _normalize(username) in load_users()


def create_user(username: str, password: str, role: str, created_by: str) -> None:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    key = _normalize(username)
    if not key:
        raise ValueError("username cannot be empty")
    users = load_users()
    if key in users:
        raise ValueError(f"a user with email {username!r} already exists")
    pw_hash, salt = hash_password(password)
    users[key] = {
        "password_hash": pw_hash,
        "salt": salt,
        "role": role,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "created_by": created_by,
    }
    save_users(users)


def authenticate(username: str, password: str) -> dict | None:
    """Returns {"username": ..., "role": ...} on success, None on failure."""
    users = load_users()
    key = _normalize(username)
    record = users.get(key)
    if not record:
        return None
    if not verify_password(password, record["password_hash"], record["salt"]):
        return None
    return {"username": key, "role": record["role"]}


def change_password(username: str, new_password: str) -> None:
    if not new_password or len(new_password) < 8:
        raise ValueError("password must be at least 8 characters")
    users = load_users()
    key = _normalize(username)
    if key not in users:
        raise ValueError(f"no such user {username!r}")
    pw_hash, salt = hash_password(new_password)
    users[key]["password_hash"] = pw_hash
    users[key]["salt"] = salt
    save_users(users)


def list_users() -> list[dict]:
    """Returns [{"username", "role", "created_at", "created_by"}, ...] —
    never includes password_hash or salt."""
    users = load_users()
    return [
        {"username": uname, "role": rec["role"],
         "created_at": rec.get("created_at", ""), "created_by": rec.get("created_by", "")}
        for uname, rec in sorted(users.items())
    ]


def ensure_bootstrap_admin(username: str, password: str) -> None:
    """Creates the given admin account if no users exist yet. Safe to call
    on every app startup — it's a no-op once at least one user exists."""
    if load_users():
        return
    create_user(username, password, "admin", created_by="system")
