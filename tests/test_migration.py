"""The one-off v5->v6 migration script (scripts/migrate_v5_to_v6.py)."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "migrate_v5_to_v6",
    Path(__file__).parent.parent / "scripts" / "migrate_v5_to_v6.py",
)
migration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(migration)


@pytest.fixture
def v5_db(tmp_path: Path) -> Path:
    """A minimal v5-shaped DB: the columns the migration touches, plus rows in
    every interesting v5 state."""
    path = tmp_path / "v5.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (5);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            rule_action TEXT,
            llm_action TEXT,
            llm_confidence REAL,
            action TEXT,
            decision_source TEXT,
            review_status TEXT NOT NULL DEFAULT 'pending'
        );
        -- 1: a confirmed trash, auto-approved (must survive untouched)
        INSERT INTO messages VALUES
            (1, 'trash', 'trash', 0.95, 'trash', 'rule+llm', 'auto_approved');
        -- 2: a v5 disagreement (action='review'), pending
        INSERT INTO messages VALUES
            (2, 'trash', 'keep', 0.7, 'review', 'rule+llm', 'pending');
        -- 3: the user's no-op bug — a 'review' row a human APPROVED in v5
        INSERT INTO messages VALUES
            (3, 'keep', 'trash', 0.98, 'review', 'rule+llm', 'approved');
        -- 4: a v5 failed classification (action='review', source='llm')
        INSERT INTO messages VALUES
            (4, NULL, 'review', 0.0, 'review', 'llm', 'pending');
        -- 5: applied row (terminal; must survive untouched)
        INSERT INTO messages VALUES
            (5, 'trash', 'trash', 0.97, 'trash', 'rule+llm', 'applied');
        """
    )
    conn.commit()
    conn.close()
    return path


def test_migrate_transforms_v5_states(v5_db: Path):
    assert migration.main([str("migrate"), str(v5_db)]) == 0

    conn = sqlite3.connect(v5_db)
    conn.row_factory = sqlite3.Row
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM messages")}

    # Renamed column with values intact for decided rows.
    assert rows[1]["staged_action"] == "trash"
    assert rows[1]["review_status"] == "auto_approved"
    assert rows[5]["staged_action"] == "trash"
    assert rows[5]["review_status"] == "applied"

    # 'review' rows became undecided, with the NULL-together invariant.
    for mid in (2, 3, 4):
        assert rows[mid]["staged_action"] is None
        assert rows[mid]["decision_source"] is None
        assert rows[mid]["llm_action"] is not None  # verdicts survive

    # The no-op approval was demoted back into the decision queue.
    assert rows[3]["review_status"] == "pending"

    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 6
    conn.close()

    # A .v5.bak copy was written and still holds the old shape.
    backup = v5_db.with_suffix(v5_db.suffix + ".v5.bak")
    assert backup.is_file()
    bconn = sqlite3.connect(backup)
    assert bconn.execute("SELECT version FROM schema_version").fetchone()[0] == 5
    bconn.close()


def test_migrate_refuses_wrong_version(v5_db: Path):
    conn = sqlite3.connect(v5_db)
    conn.execute("UPDATE schema_version SET version=4")
    conn.commit()
    conn.close()
    assert migration.main(["migrate", str(v5_db)]) == 1


def test_migrate_refuses_missing_db(tmp_path: Path):
    assert migration.main(["migrate", str(tmp_path / "nope.db")]) == 1


def test_migrate_is_not_rerunnable(v5_db: Path):
    # Second run refuses (version is already 6) instead of corrupting.
    assert migration.main(["migrate", str(v5_db)]) == 0
    assert migration.main(["migrate", str(v5_db)]) == 1
