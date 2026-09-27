"""Small, concurrency-safe SQLite cache for provider text and action results."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from pathlib import Path


def content_key(provider: str, model: str, prompt_version: str,
                system: str, user: str) -> str:
    value = "\0".join((provider, model, prompt_version, system, user))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ResponseCache:
    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            state = Path(os.environ.get("VERA_STATE_DIR", Path(__file__).resolve().parents[2] / "vera_state"))
            path = state / "llm_cache.sqlite3"
        self.path = Path(path)
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=3.0)
        db.execute("CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        return db

    def get(self, key: str) -> str | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT value FROM responses WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else None

    def put(self, key: str, value: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO responses(key,value) VALUES (?,?)", (key, value))

    def clear(self) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM responses")
