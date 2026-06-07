"""SQLite connection factory and schema management."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 1


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with the project's standard PRAGMAs."""
    path = Path(db_path)
    if path.parent and str(path.parent) != ".":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema if missing (idempotent)."""
    ddl = (
        resources.files("local_llm_email_cleaner")
        .joinpath("schema.sql")
        .read_text("utf-8")
    )
    conn.executescript(ddl)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
        )
    conn.commit()


def open_db(db_path: Path | str) -> sqlite3.Connection:
    """Connect and ensure the schema exists."""
    conn = connect(db_path)
    init_db(conn)
    return conn
