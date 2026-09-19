"""SQLite-backed persistent leaderboard with WAL and bounded rows.

Every access opens a short-lived connection guarded by a module lock: SQLite
serialises writers that way even from multiple asyncio/thread contexts, and
the row cap keeps the file from growing without bound.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    score INTEGER NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_score ON scores (score DESC);
"""

_trim_lock = threading.Lock()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def submit(self, name: str, score: int) -> None:
        if score < 1:
            return
        with _trim_lock:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO scores (name, score, ts) VALUES (?, ?, ?)",
                    (name[:24], score, now_iso()),
                )
                conn.execute(
                    "DELETE FROM scores WHERE id NOT IN "
                    "(SELECT id FROM scores ORDER BY score DESC LIMIT 500)"
                )

    def top(self, limit: int = 10) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, score, ts FROM scores ORDER BY score DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"name": r[0], "score": r[1], "ts": r[2]} for r in rows]

    def deaths(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]

    def clear(self) -> None:
        with _trim_lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM scores")


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")