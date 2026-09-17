"""SQLite storage for immutable, versioned flag configurations.

Each published configuration is stored as a single JSON document keyed by a
client-provided version. Rows are never updated or deleted, so re-publishing a
new version cannot change the result of any evaluation pinned to an old
version.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = os.environ.get("FLAG_DB_PATH", "data/flags.db")

_create_lock = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db_path() -> str:
    return os.environ.get("FLAG_DB_PATH", DEFAULT_DB_PATH)


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open a SQLite connection suitable for the request scope."""
    path = db_path or get_db_path()
    parent = os.path.dirname(os.path.abspath(path))
    Path(parent).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS config_versions (
            version      TEXT PRIMARY KEY,
            payload      TEXT NOT NULL,
            flag_count   INTEGER NOT NULL,
            created_at   TEXT NOT NULL
        )
        """
    )
    conn.commit()


def insert_version(
    conn: sqlite3.Connection, version: str, config: dict[str, Any]
) -> Optional[str]:
    """Persist a new immutable config version.

    Returns the created_at timestamp, or ``None`` if the version already
    exists. The INSERT is wrapped in an IMMEDIATE transaction so concurrent
    publishes of the same version cannot both succeed.
    """
    flags = config.get("flags", {})
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True)
    with _create_lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM config_versions WHERE version = ?", (version,)
            ).fetchone()
            if existing is not None:
                conn.rollback()
                return None
            created_at = _utcnow()
            conn.execute(
                "INSERT INTO config_versions (version, payload, flag_count, created_at)"
                " VALUES (?, ?, ?, ?)",
                (version, payload, len(flags), created_at),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return created_at


def get_version(conn: sqlite3.Connection, version: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT version, payload, flag_count, created_at"
        " FROM config_versions WHERE version = ?",
        (version,),
    ).fetchone()
    if row is None:
        return None
    return {
        "version": row["version"],
        "config": json.loads(row["payload"]),
        "flag_count": row["flag_count"],
        "created_at": row["created_at"],
    }


def list_versions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT version, flag_count, created_at"
        " FROM config_versions ORDER BY created_at ASC, version ASC"
    ).fetchall()
    return [
        {
            "version": row["version"],
            "flag_count": row["flag_count"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
