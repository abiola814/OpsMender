"""SQLite storage.

One connection shared by the web handlers and the background scheduler, guarded by a lock.
Every query is small, so a single writer in WAL mode is plenty for this workload.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS servers (
    uuid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ip TEXT,
    port INTEGER,
    ssh_user TEXT,
    reachable INTEGER,
    usable INTEGER,
    raw TEXT,
    updated_at REAL,
    metrics_enabled INTEGER NOT NULL DEFAULT 0,
    probe_host TEXT,
    probe_port INTEGER,
    host_key TEXT,
    last_probe_at REAL,
    last_probe_error TEXT,
    probe_failures INTEGER NOT NULL DEFAULT 0,
    last_snapshot TEXT,      -- raw probe JSON, kept to compute rates on the next poll
    current_metrics TEXT     -- computed figures from the latest poll
);

CREATE TABLE IF NOT EXISTS resources (
    uuid TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT,
    server_uuid TEXT,
    project TEXT,
    environment TEXT,
    fqdn TEXT,
    git_repository TEXT,
    git_branch TEXT,
    raw TEXT,
    updated_at REAL,
    status_changed_at REAL,
    missing INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS deployments (
    uuid TEXT PRIMARY KEY,
    app_uuid TEXT,
    app_name TEXT,
    status TEXT,
    commit_sha TEXT,
    created_at TEXT,
    updated_at TEXT,
    seen_at REAL,
    final INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    title TEXT NOT NULL,
    summary TEXT,
    resource_kind TEXT,
    resource_uuid TEXT,
    resource_name TEXT,
    server_uuid TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    evidence TEXT,
    findings TEXT,
    ai_diagnosis TEXT,
    ai_model TEXT,
    ai_at REAL,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_incidents_fp ON incidents(fingerprint, status);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    resource_uuid TEXT,
    incident_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS metric_samples (
    server_uuid TEXT NOT NULL,
    ts REAL NOT NULL,
    cpu_pct REAL,
    load1 REAL,
    mem_used INTEGER,
    mem_total INTEGER,
    swap_used INTEGER,
    disk_used_pct REAL,
    disk_used INTEGER,
    disk_total INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples ON metric_samples(server_uuid, ts);

CREATE TABLE IF NOT EXISTS metric_rollups (
    server_uuid TEXT NOT NULL,
    bucket_ts REAL NOT NULL,
    cpu_avg REAL,
    cpu_max REAL,
    mem_pct_avg REAL,
    mem_pct_max REAL,
    disk_used_pct REAL,
    PRIMARY KEY (server_uuid, bucket_ts)
);

CREATE TABLE IF NOT EXISTS container_samples (
    server_uuid TEXT NOT NULL,
    ts REAL NOT NULL,
    name TEXT NOT NULL,
    cpu_pct REAL,
    mem_bytes INTEGER,
    restarts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_csamples ON container_samples(server_uuid, name, ts);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    username TEXT,
    action TEXT NOT NULL,
    target_kind TEXT,
    target_uuid TEXT,
    target_name TEXT,
    result TEXT NOT NULL,
    detail TEXT
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, rows)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return row[0] if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # Small helpers shared across modules.

    def add_event(self, kind: str, message: str, level: str = "info",
                  resource_uuid: str | None = None, incident_id: int | None = None) -> None:
        self.execute(
            "INSERT INTO events (ts, kind, level, message, resource_uuid, incident_id) VALUES (?,?,?,?,?,?)",
            (time.time(), kind, level, message, resource_uuid, incident_id),
        )

    def audit(self, username: str | None, action: str, result: str, target_kind: str | None = None,
              target_uuid: str | None = None, target_name: str | None = None, detail: str | None = None) -> None:
        self.execute(
            "INSERT INTO audit (ts, username, action, target_kind, target_uuid, target_name, result, detail)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), username, action, target_kind, target_uuid, target_name, result, detail),
        )


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)
