"""SQLite storage for immutable, versioned flag configurations.

Each published configuration is stored as a single JSON document keyed by a
client-provided version. Rows are never updated or deleted, so re-publishing a
new version cannot change the result of any evaluation pinned to an old
version.

Environments (``production``, ``staging``, ...) hold a *pointer* to one of the
immutable versions. The pointer carries a monotonically increasing
``revision`` used for optimistic concurrency control: a pointer may only be
created with ``expected_revision == 0`` and only be moved by a caller that
presents the revision it last observed. Every check and the write happen in a
single ``BEGIN IMMEDIATE`` transaction, so a concurrent update loses the race
with ``409 stale_revision`` instead of silently overwriting the pointer.
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


class StaleRevision(Exception):
    """Raised when an environment write uses an outdated ``expected_revision``.

    This covers both the "create only with revision 0" rule (environment
    already exists but ``expected_revision == 0``) and lost-update detection
    (the observed revision no longer matches the current one). No row is
    modified when this is raised.
    """

    def __init__(self, name: str, expected: int, current: Optional[int]):
        self.name = name
        self.expected = expected
        self.current = current
        if current is None:
            message = f"environment {name!r} already exists; create requires expected_revision=0"
        else:
            message = (
                f"expected_revision {expected} is stale for environment {name!r};"
                f" current revision is {current}"
            )
        super().__init__(message)


class VersionNotFound(Exception):
    """Raised when a pointer targets a config version that was never published."""

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
    # Environment pointers. ``revision`` starts at 1 on creation and is
    # incremented by every successful pointer update; it implements
    # optimistic concurrency control together with expected_revision.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS environments (
            name         TEXT PRIMARY KEY,
            version      TEXT NOT NULL,
            revision     INTEGER NOT NULL,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            FOREIGN KEY (version) REFERENCES config_versions(version)
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
# Environment pointers
# ---------------------------------------------------------------------------


def _env_row(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT name, version, revision, created_at, updated_at"
        " FROM environments WHERE name = ?",
        (name,),
    ).fetchone()


def upsert_environment(
    conn: sqlite3.Connection, name: str, version: str, expected_revision: int
) -> tuple[dict[str, Any], bool]:
    """Create or move the environment pointer with optimistic concurrency.

    The whole decision runs in one ``BEGIN IMMEDIATE`` transaction:

      * create: the environment must not exist and ``expected_revision`` must
        be 0; the new pointer starts at revision 1;
      * update: the stored revision must equal ``expected_revision`` and the
        target version must exist; the revision is incremented by one.

    Returns ``(environment, created)``. Raises :class:`StaleRevision` when the
    create/update precondition fails (the pointer is left untouched) and
    :class:`VersionNotFound` when the target version was never published.
    """
    now = _utcnow()
    created = False
    try:
        conn.execute("BEGIN IMMEDIATE")

        existing = _env_row(conn, name)

        if existing is None:
            if expected_revision != 0:
                raise StaleRevision(name, expected_revision, None)
            # Confirm the referenced version exists before creating the row;
            # the FOREIGN KEY would also reject it, but we want a clean
            # version_not_found rather than a raw integrity error.
            if not _version_exists(conn, version):
                raise VersionNotFound(version)
            conn.execute(
                "INSERT INTO environments (name, version, revision, created_at, updated_at)"
                " VALUES (?, ?, 1, ?, ?)",
                (name, version, now, now),
            )
            created = True
        else:
            current_revision = existing["revision"]
            # Revision is checked first: a stale writer must not be able to
            # distinguish 404 from 409 or mutate anything.
            if current_revision != expected_revision:
                raise StaleRevision(name, expected_revision, current_revision)
            if not _version_exists(conn, version):
                raise VersionNotFound(version)
            conn.execute(
                "UPDATE environments SET version = ?, revision = revision + 1, updated_at = ?"
                " WHERE name = ? AND revision = ?",
                (version, now, name, current_revision),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    row = _env_row(conn, name)
    env = {
        "name": row["name"],
        "version": row["version"],
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    return env, created


def get_environment(conn: sqlite3.Connection, name: str) -> Optional[dict[str, Any]]:
    row = _env_row(conn, name)
    if row is None:
        return None
    return {
        "name": row["name"],
        "version": row["version"],
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_environment_snapshot(
    conn: sqlite3.Connection, name: str
) -> Optional[dict[str, Any]]:
    """Return the pointer and its pinned config in one read.

    A single SELECT gives the evaluation handler a consistent pointer
    snapshot: the version payload returned here is the one the pointer named
    at read time, even if another transaction moves the pointer afterwards.
    """
    row = conn.execute(
        "SELECT e.name        AS name,"
        "       e.version     AS version,"
        "       e.revision    AS revision,"
        "       e.created_at  AS created_at,"
        "       e.updated_at  AS updated_at,"
        "       c.payload     AS payload"
        " FROM environments e"
        " JOIN config_versions c ON c.version = e.version"
        " WHERE e.name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return {
        "name": row["name"],
        "version": row["version"],
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "config": json.loads(row["payload"]),
    }


def _version_exists(conn: sqlite3.Connection, version: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM config_versions WHERE version = ?", (version,)
        ).fetchone()
        is not None
    )
