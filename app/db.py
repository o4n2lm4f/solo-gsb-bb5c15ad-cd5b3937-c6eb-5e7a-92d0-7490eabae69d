"""SQLite storage for immutable, versioned flag configurations.

Each published configuration is stored as a single JSON document keyed by a
client-provided version. Rows are never updated or deleted, so re-publishing a
new version cannot change the result of any evaluation pinned to an old
version.

On top of the immutable versions, named *environment pointers* map an
environment (e.g. ``prod``) to one published version. Each pointer carries a
monotonically increasing ``revision`` used for optimistic concurrency: writers
must present the revision they based their write on, and the check + version
existence check + pointer move all happen inside a single BEGIN IMMEDIATE
transaction.
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
_pointer_lock = threading.Lock()


class StaleRevisionError(Exception):
    """expected_revision does not match the pointer's current revision."""

    def __init__(self, name: str, expected: int, current: int):
        self.name = name
        self.expected = expected
        self.current = current
        super().__init__(
            f"environment {name!r}: expected revision {expected}, current is {current}"
        )


class VersionNotFoundError(Exception):
    """The config version a pointer should reference does not exist."""

    def __init__(self, version: str):
        self.version = version
        super().__init__(f"config version {version!r} not found")


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS environments (
            name         TEXT PRIMARY KEY,
            version      TEXT NOT NULL,
            revision     INTEGER NOT NULL,
            updated_at   TEXT NOT NULL
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


# ---------------------------------------------------------------------------
# Environment pointers (name -> published version, with optimistic revision)
# ---------------------------------------------------------------------------


def get_environment(conn: sqlite3.Connection, name: str) -> Optional[dict[str, Any]]:
    """Return the current pointer snapshot for an environment, or None."""
    row = conn.execute(
        "SELECT name, version, revision, updated_at"
        " FROM environments WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return {
        "name": row["name"],
        "version": row["version"],
        "revision": row["revision"],
        "updated_at": row["updated_at"],
    }


def set_environment(
    conn: sqlite3.Connection, name: str, version: str, expected_revision: int
) -> dict[str, Any]:
    """Create or move an environment pointer with optimistic concurrency.

    A missing environment is treated as revision 0, so ``expected_revision=0``
    creates the pointer (at revision 1) and any higher expectation on a
    missing environment is stale. The revision check, the config-version
    existence check and the pointer move all run inside one BEGIN IMMEDIATE
    transaction, so a concurrent writer either serializes behind this one (and
    then fails its own revision check) or never observes a half-applied state.
    On any failure the transaction is rolled back and the pointer is left
    untouched.

    Raises :class:`StaleRevisionError` on a revision mismatch and
    :class:`VersionNotFoundError` when the target version was never published.
    Returns the new pointer snapshot on success.
    """
    with _pointer_lock:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision FROM environments WHERE name = ?", (name,)
            ).fetchone()
            current = row["revision"] if row is not None else 0
            if expected_revision != current:
                raise StaleRevisionError(name, expected_revision, current)
            exists = conn.execute(
                "SELECT 1 FROM config_versions WHERE version = ?", (version,)
            ).fetchone()
            if exists is None:
                raise VersionNotFoundError(version)
            new_revision = current + 1
            updated_at = _utcnow()
            conn.execute(
                "INSERT INTO environments (name, version, revision, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET"
                "   version = excluded.version,"
                "   revision = excluded.revision,"
                "   updated_at = excluded.updated_at",
                (name, version, new_revision, updated_at),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {
        "name": name,
        "version": version,
        "revision": new_revision,
        "updated_at": updated_at,
    }
