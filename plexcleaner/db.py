"""SQLite persistence. Plain sqlite3 — the schema is small and the queries are simple."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    trigger      TEXT NOT NULL DEFAULT 'manual',
    stats        TEXT NOT NULL DEFAULT '{}',
    errors       TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS media_item (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id           INTEGER NOT NULL REFERENCES scan(id) ON DELETE CASCADE,
    kind              TEXT NOT NULL,              -- movie | show
    title             TEXT NOT NULL,
    year              INTEGER,
    plex_rating_key   TEXT,
    plex_section_id   TEXT,
    plex_library      TEXT,
    tmdb_id           TEXT,
    tvdb_id           TEXT,
    imdb_id           TEXT,
    added_at          TEXT,
    last_watched_at   TEXT,
    play_count        INTEGER NOT NULL DEFAULT 0,
    watcher_count     INTEGER NOT NULL DEFAULT 0,
    size_bytes        INTEGER NOT NULL DEFAULT 0,
    episode_count     INTEGER NOT NULL DEFAULT 0,
    arr_instance      TEXT,
    arr_id            INTEGER,
    arr_status        TEXT,
    arr_monitored     INTEGER NOT NULL DEFAULT 0,
    arr_tags          TEXT NOT NULL DEFAULT '[]',
    seerr_media_id    INTEGER,
    seerr_requested_by TEXT,
    seerr_requested_at TEXT,
    labels            TEXT NOT NULL DEFAULT '[]',
    collections       TEXT NOT NULL DEFAULT '[]',
    verdict           TEXT NOT NULL DEFAULT 'keep',   -- candidate | keep | protected
    reasons           TEXT NOT NULL DEFAULT '[]',
    detail            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_media_scan    ON media_item(scan_id);
CREATE INDEX IF NOT EXISTS idx_media_verdict ON media_item(scan_id, verdict);

CREATE TABLE IF NOT EXISTS user_account (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id           INTEGER NOT NULL REFERENCES scan(id) ON DELETE CASCADE,
    username          TEXT NOT NULL,
    email             TEXT,
    title             TEXT,
    plex_user_id      TEXT,
    plex_share_id     TEXT,
    tautulli_user_id  TEXT,
    seerr_user_id     INTEGER,
    is_admin          INTEGER NOT NULL DEFAULT 0,
    is_home           INTEGER NOT NULL DEFAULT 0,
    last_seen_at      TEXT,
    last_login_at     TEXT,
    plays             INTEGER NOT NULL DEFAULT 0,
    request_count     INTEGER NOT NULL DEFAULT 0,
    verdict           TEXT NOT NULL DEFAULT 'keep',
    reasons           TEXT NOT NULL DEFAULT '[]',
    detail            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_user_scan ON user_account(scan_id);

-- Permanent "never propose this again" list, keyed by a stable identity.
CREATE TABLE IF NOT EXISTS protection (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,          -- media | user
    ref        TEXT NOT NULL,          -- protection key (see rules.protection_key)
    label      TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(kind, ref)
);

CREATE TABLE IF NOT EXISTS plan (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id     INTEGER REFERENCES scan(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL,
    executed_at TEXT,
    status      TEXT NOT NULL DEFAULT 'draft',   -- draft | executing | done | failed | cancelled
    dry_run     INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT NOT NULL DEFAULT 'web',
    summary     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS plan_item (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     INTEGER NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,          -- media | user
    title       TEXT NOT NULL,
    ref         TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    steps       TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | done | failed | skipped
    result      TEXT NOT NULL DEFAULT '{}',
    snapshot    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_plan_item ON plan_item(plan_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL DEFAULT 'system',
    service   TEXT NOT NULL,
    action    TEXT NOT NULL,
    target    TEXT NOT NULL DEFAULT '',
    dry_run   INTEGER NOT NULL DEFAULT 1,
    ok        INTEGER NOT NULL DEFAULT 1,
    plan_id   INTEGER,
    detail    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # -- generic helpers -------------------------------------------------
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.connect() as conn:
            return conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, table: str, data: dict[str, Any]) -> int:
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        with self.connect() as conn:
            cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))
            return int(cur.lastrowid or 0)

    def insert_many(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        cols = list(rows[0])
        marks = ", ".join("?" for _ in cols)
        with self.connect() as conn:
            conn.executemany(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})",
                [tuple(r.get(c) for c in cols) for r in rows],
            )

    # -- settings --------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM setting WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO setting (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # -- audit -----------------------------------------------------------
    def audit(
        self,
        *,
        service: str,
        action: str,
        target: str = "",
        actor: str = "system",
        dry_run: bool = True,
        ok: bool = True,
        plan_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> int:
        return self.insert("audit_log", {
            "ts": utcnow(),
            "actor": actor,
            "service": service,
            "action": action,
            "target": target[:500],
            "dry_run": int(dry_run),
            "ok": int(ok),
            "plan_id": plan_id,
            "detail": json.dumps(detail or {}, default=str)[:20000],
        })

    # -- protections -----------------------------------------------------
    def protections(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return self.query("SELECT * FROM protection WHERE kind = ? ORDER BY created_at DESC", (kind,))
        return self.query("SELECT * FROM protection ORDER BY created_at DESC")

    def protected_refs(self, kind: str) -> set[str]:
        return {r["ref"] for r in self.query("SELECT ref FROM protection WHERE kind = ?", (kind,))}

    def add_protection(self, kind: str, ref: str, label: str = "", note: str = "") -> None:
        self.execute(
            "INSERT INTO protection (kind, ref, label, note, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, ref) DO UPDATE SET note = excluded.note, label = excluded.label",
            (kind, ref, label, note, utcnow()),
        )

    def remove_protection(self, kind: str, ref: str) -> None:
        self.execute("DELETE FROM protection WHERE kind = ? AND ref = ?", (kind, ref))

    # -- scans -----------------------------------------------------------
    def latest_scan(self) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM scan WHERE status = 'complete' ORDER BY id DESC LIMIT 1")

    def prune(self, retention_days: int) -> dict[str, int]:
        """Drop old audit rows and old scans, always keeping the newest scan."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(retention_days, 1))).isoformat()
        latest = self.query_one("SELECT id FROM scan ORDER BY id DESC LIMIT 1")
        keep_id = latest["id"] if latest else -1
        with self.connect() as conn:
            audits = conn.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,)).rowcount
            scans = conn.execute(
                "DELETE FROM scan WHERE started_at < ? AND id != ?", (cutoff, keep_id)
            ).rowcount
        return {"audit_rows": max(audits, 0), "scans": max(scans, 0)}
