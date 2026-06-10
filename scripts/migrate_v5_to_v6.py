"""One-off migration: schema v5 -> v6 (action -> staged_action).

Preserves an existing v5 database — most importantly its stored LLM verdicts
(llm_* columns), which represent real classification time — instead of the
usual delete-and-rebuild. Run it YOURSELF against your working DB:

    uv run python scripts/migrate_v5_to_v6.py            # default: data/email.db
    uv run python scripts/migrate_v5_to_v6.py path/to.db

What it does (a `<db>.v5.bak` copy is written first):
1. Renames messages.action -> staged_action.
2. v6 semantics: 'review' was never a real action — those rows become
   undecided (staged_action NULL, decision_source NULL) and land in the
   review UI's needs-decision queue.
3. Demotes approvals that now point at nothing (review_status approved/
   auto_approved with no staged_action) back to 'pending' — these were
   silent no-ops in v5.
4. Bumps schema_version to 6.

This is the project's only migration; the general rule (version mismatch =>
fresh init+ingest) still stands for every other bump.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

EXPECTED_VERSION = 5
TARGET_VERSION = 6


def migrate(conn: sqlite3.Connection) -> dict[str, int]:
    """Apply the v5->v6 transformation; returns per-step row counts.

    The caller is responsible for the version guard and backup.
    """
    counts: dict[str, int] = {}
    conn.execute("ALTER TABLE messages RENAME COLUMN action TO staged_action")
    cur = conn.execute(
        "UPDATE messages SET staged_action=NULL, decision_source=NULL "
        "WHERE staged_action='review'"
    )
    counts["review_rows_now_undecided"] = cur.rowcount
    cur = conn.execute(
        "UPDATE messages SET review_status='pending' "
        "WHERE staged_action IS NULL "
        "  AND review_status IN ('approved', 'auto_approved')"
    )
    counts["orphaned_approvals_demoted"] = cur.rowcount
    conn.execute("UPDATE schema_version SET version=?", (TARGET_VERSION,))
    conn.commit()
    return counts


def main(argv: list[str]) -> int:
    db_path = Path(argv[1]) if len(argv) > 1 else Path("data/email.db")
    if not db_path.is_file():
        print(f"No database at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    version = conn.execute("SELECT version FROM schema_version").fetchone()
    if version is None or version["version"] != EXPECTED_VERSION:
        found = version["version"] if version else "missing"
        print(
            f"Refusing to migrate: {db_path} is schema v{found}, this script "
            f"only migrates v{EXPECTED_VERSION} -> v{TARGET_VERSION}.",
            file=sys.stderr,
        )
        conn.close()
        return 1

    backup = db_path.with_suffix(db_path.suffix + ".v5.bak")
    shutil.copy2(db_path, backup)
    print(f"Backup written: {backup}")

    counts = migrate(conn)
    needs_decision = conn.execute(
        "SELECT COUNT(*) FROM messages "
        "WHERE staged_action IS NULL AND llm_action IS NOT NULL"
    ).fetchone()[0]
    conn.close()

    print(f"Migrated {db_path} to schema v{TARGET_VERSION}:")
    for step, n in counts.items():
        print(f"  {step}: {n}")
    print(
        f"Rows now awaiting your decision in the review UI: {needs_decision}\n"
        "Open it with `uv run email-cleaner review` — the 'Needs decision' "
        "preset lists them."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
