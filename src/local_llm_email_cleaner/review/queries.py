"""Parameterized SQL shared by the Streamlit app and the CSV export."""

from __future__ import annotations

from ..models import (
    ACTIONABLE_ACTIONS,
    APPROVABLE_STATUSES,
    LLM_CLASSIFIERS,
    sql_in_list,
)

MESSAGE_COLS = """
    id, date_utc, from_addr, from_domain, subject, ai_category, ai_confidence,
    ai_reason, staged_label, proposed_action, review_status, classified_by,
    size_bytes, has_attachments
"""

PROPOSED_TRASH = f"""
SELECT {MESSAGE_COLS} FROM messages
WHERE proposed_action='trash' AND review_status IN ({{statuses}})
ORDER BY from_domain, date_epoch
"""

AUTO_APPROVED = f"""
SELECT {MESSAGE_COLS} FROM messages
WHERE review_status='auto_approved'
ORDER BY from_domain, date_epoch
"""

BY_SENDER = """
SELECT from_addr, COUNT(*) AS messages,
       SUM(CASE WHEN proposed_action='trash' THEN 1 ELSE 0 END) AS proposed_trash,
       SUM(CASE WHEN proposed_action='archive' THEN 1 ELSE 0 END) AS proposed_archive,
       SUM(CASE WHEN review_status='pending' THEN 1 ELSE 0 END) AS pending,
       ROUND(SUM(size_bytes) / 1048576.0, 1) AS total_mb
FROM messages
WHERE from_addr IS NOT NULL
GROUP BY from_addr
ORDER BY messages DESC
"""

BY_DOMAIN = """
SELECT from_domain, COUNT(*) AS messages,
       SUM(CASE WHEN proposed_action='trash' THEN 1 ELSE 0 END) AS proposed_trash,
       SUM(CASE WHEN proposed_action='archive' THEN 1 ELSE 0 END) AS proposed_archive,
       SUM(CASE WHEN review_status='pending' THEN 1 ELSE 0 END) AS pending,
       ROUND(SUM(size_bytes) / 1048576.0, 1) AS total_mb
FROM messages
WHERE from_domain IS NOT NULL
GROUP BY from_domain
ORDER BY messages DESC
"""

LARGEST_SENDERS = """
SELECT from_addr, COUNT(*) AS messages,
       ROUND(SUM(size_bytes) / 1048576.0, 1) AS total_mb
FROM messages
WHERE from_addr IS NOT NULL
GROUP BY from_addr
ORDER BY SUM(size_bytes) DESC
LIMIT 100
"""

OLDEST_PROMOS = f"""
SELECT {MESSAGE_COLS} FROM messages
WHERE staged_label IN ('DELETE_CANDIDATE', 'UNSUBSCRIBE_CANDIDATE')
ORDER BY date_epoch ASC
LIMIT 500
"""

UNCERTAIN = f"""
SELECT {MESSAGE_COLS} FROM messages
WHERE classified_by IN ({sql_in_list(LLM_CLASSIFIERS)}) AND ai_confidence < ?
ORDER BY ai_confidence ASC
"""

FTS_SEARCH = f"""
SELECT {MESSAGE_COLS} FROM messages
WHERE id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)
ORDER BY date_epoch DESC
LIMIT 500
"""

STATUS_COUNTS = """
SELECT review_status, proposed_action, COUNT(*) AS n
FROM messages GROUP BY review_status, proposed_action ORDER BY n DESC
"""

# Browse messages filtered by status/action or staged label; a NULL named
# param disables that filter.
BY_STATUS_WHERE = """
WHERE (:status IS NULL OR review_status = :status)
  AND (:action IS NULL OR proposed_action = :action)
  AND (:label IS NULL OR staged_label = :label)
"""

BY_STATUS = f"""
SELECT {MESSAGE_COLS} FROM messages
{BY_STATUS_WHERE}
ORDER BY date_epoch DESC
LIMIT 500
"""

BY_STATUS_COUNT = f"""
SELECT COUNT(*) FROM messages
{BY_STATUS_WHERE}
"""

STAGED_COUNTS = """
SELECT staged_label, COUNT(*) AS n FROM messages GROUP BY staged_label ORDER BY n DESC
"""

MESSAGE_DETAIL = """
SELECT * FROM messages WHERE id = ?
"""

RULE_HITS_FOR_MESSAGE = """
SELECT rule_name, rule_kind, outcome FROM rule_hits WHERE message_id = ?
"""

ACTIONS_FOR_MESSAGE = """
SELECT action, dry_run, status, match_method, match_confirmed, error, requested_at, completed_at
FROM actions WHERE message_id = ? ORDER BY id DESC
"""

# Export: the approved action table as CSV. The WHERE must stay in lockstep
# with the runner's _SELECT_APPROVED — both build it from the same constants.
EXPORT_ACTIONS = f"""
SELECT gmail_msgid AS gmail_message_id, rfc_message_id, proposed_action AS action,
       COALESCE(ai_reason, ai_category, 'rule match') AS reason, ai_confidence AS confidence,
       review_status, from_addr, subject, date_utc
FROM messages
WHERE review_status IN ({sql_in_list(APPROVABLE_STATUSES)})
  AND proposed_action IN ({sql_in_list(ACTIONABLE_ACTIONS)})
ORDER BY from_domain, date_epoch
"""


def in_clause(values: list[str]) -> str:
    """Quote a list for an IN (...) clause of trusted, static strings."""
    return ",".join(f"'{v}'" for v in values)


def update_status_if_pending(conn, ids: list[int], status: str) -> int:
    """Set review_status on ids that are STILL pending; returns rows changed.

    Used by the review UI's group bulk actions: combined with acting on a
    render-time snapshot of ids, the pending guard ensures a row that changed
    state between render and click (applied, approved elsewhere, skipped)
    is never silently overwritten.
    """
    if not ids:
        return 0
    cur = conn.executemany(
        "UPDATE messages SET review_status=? WHERE id=? AND review_status='pending'",
        [(status, i) for i in ids],
    )
    conn.commit()
    return cur.rowcount
