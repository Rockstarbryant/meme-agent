"""Local durable state: config cache, portfolio, idempotency, command dedupe and the event OUTBOX (survives offline periods/restarts)."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from app.events.bus import IdempotencyStore


class LocalStore:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, result TEXT);
            CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS outbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT, event TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_outbox_dedupe ON outbox(dedupe_key);
        """)
        if str(path) != ":memory:":
            os.chmod(path, 0o600)

    def kv_get(self, key: str) -> dict | None:
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def kv_set(self, key: str, value: dict) -> None:
        self.db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, default=str)))

    # idempotency (atomic claim)
    def idem_claim(self, key: str) -> bool:
        return self.db.execute("INSERT OR IGNORE INTO idempotency(key,result) VALUES(?,NULL)", (key,)).rowcount == 1

    def idem_release(self, key: str) -> None:
        self.db.execute("DELETE FROM idempotency WHERE key=?", (key,))

    def idem_complete(self, key: str, result: dict) -> None:
        self.db.execute("UPDATE idempotency SET result=? WHERE key=?", (json.dumps(result, default=str), key))

    def idem_get(self, key: str) -> dict | None:
        r = self.db.execute("SELECT result FROM idempotency WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r and r[0] else None

    def idem_seed(self, key: str, result: dict | None = None) -> None:
        """Restore a remotely authoritative execution claim after worker restart."""
        self.db.execute("INSERT OR IGNORE INTO idempotency(key,result) VALUES(?,?)", (key, json.dumps(result, default=str) if result is not None else None))

    # outbox
    def outbox_add(self, event: dict, dedupe_key: str | None = None) -> int:
        if dedupe_key:  # coalesce high-frequency updates: only the newest unsent one matters
            self.db.execute("DELETE FROM outbox WHERE dedupe_key=?", (dedupe_key,))
        return self.db.execute("INSERT INTO outbox(dedupe_key,event) VALUES(?,?)", (dedupe_key, json.dumps(event, default=str))).lastrowid or 0

    def outbox_pending(self, limit: int = 100) -> list[tuple[int, dict]]:
        return [(r[0], json.loads(r[1])) for r in self.db.execute("SELECT seq,event FROM outbox ORDER BY seq LIMIT ?", (limit,))]

    def outbox_ack(self, up_to_seq: int) -> None:
        self.db.execute("DELETE FROM outbox WHERE seq<=?", (up_to_seq,))

    def outbox_size(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    # remote commands: execute each at most once
    def command_seen(self, cid: str) -> bool:
        return self.db.execute("SELECT 1 FROM commands WHERE id=?", (cid,)).fetchone() is not None

    def mark_command(self, cid: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO commands(id) VALUES(?)", (cid,))

    def close(self) -> None:
        self.db.close()


class SqliteIdempotencyStore(IdempotencyStore):
    """Restart-safe duplicate-order protection that needs no Redis on the user's machine."""

    def __init__(self, store: LocalStore):
        self.s = store

    async def claim(self, key: str) -> bool: return self.s.idem_claim(key)
    async def release(self, key: str) -> None: self.s.idem_release(key)
    async def complete(self, key: str, result: dict) -> None: self.s.idem_complete(key, result)
    async def get(self, key: str) -> dict | None: return self.s.idem_get(key)
