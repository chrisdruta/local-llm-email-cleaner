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

STATUS_COUNTS = """
SELECT review_status, proposed_action, COUNT(*) AS n
FROM messages GROUP BY review_status, proposed_action ORDER BY n DESC
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


# Unified-browser query builder. Composes a WHERE from any combination of
# filters. Stable, deterministic ORDER (id tie-break) so st.dataframe positional
# row selection stays in sync across reruns.
_BROWSER_ORDER_DEFAULT = "date_epoch DESC, id DESC"
_BROWSER_ORDER_OLDEST = "date_epoch ASC, id ASC"
BROWSER_LIMIT = 500


def _message_where(filters: dict) -> tuple[str, list]:
    """Build a parameterized WHERE for `messages` from a filter dict.

    Every user-supplied value is BOUND (appended to ``params``); only ``?``
    placeholder counts and the hard-coded column-name literals below are ever
    interpolated — never a user string. An absent/empty filter key contributes
    no clause (mirrors the old BY_STATUS NULL-disables convention).

    Recognized keys (all optional):
        fts             : str        -> messages_fts MATCH
        review_status   : list[str]  -> review_status IN (...)
        proposed_action : list[str]  -> proposed_action IN (...)
        staged_label    : list[str]  -> staged_label IN (...)
        ai_category     : list[str]  -> ai_category IN (...)
        conf_lo, conf_hi: float      -> LLM-classified AND ai_confidence >=/<= ?
        date_from, date_to: int      -> date_epoch >=/<= ? (unix seconds, UTC)
        from_addr       : str        -> from_addr LIKE %term%
        from_domain     : str        -> from_domain LIKE %term%
        has_attachments : bool       -> has_attachments = 1 (only when True)
    """
    where: list[str] = []
    params: list = []

    fts = (filters.get("fts") or "").strip()
    if fts:
        where.append(
            "id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)"
        )
        params.append(fts)

    def _in(col: str, values) -> None:
        # col is a hard-coded literal from the call sites below — never user input.
        values = list(values or [])
        if values:
            where.append(f"{col} IN ({','.join('?' for _ in values)})")
            params.extend(values)

    _in("review_status", filters.get("review_status"))
    _in("proposed_action", filters.get("proposed_action"))
    _in("staged_label", filters.get("staged_label"))
    _in("ai_category", filters.get("ai_category"))

    lo, hi = filters.get("conf_lo"), filters.get("conf_hi")
    if lo is not None or hi is not None:
        # Confidence is only meaningful for LLM-classified rows.
        where.append(f"classified_by IN ({sql_in_list(LLM_CLASSIFIERS)})")
        if lo is not None:
            where.append("ai_confidence >= ?")
            params.append(lo)
        if hi is not None:
            where.append("ai_confidence <= ?")
            params.append(hi)

    # Date range bounds the whole result set server-side (epoch seconds, UTC),
    # so sorting/scoping by date works across the entire DB — not just the
    # capped page the grid happens to hold.
    date_from, date_to = filters.get("date_from"), filters.get("date_to")
    if date_from is not None:
        where.append("date_epoch >= ?")
        params.append(date_from)
    if date_to is not None:
        where.append("date_epoch <= ?")
        params.append(date_to)

    for col in ("from_addr", "from_domain"):
        term = (filters.get(col) or "").strip()
        if term:
            where.append(f"{col} LIKE ?")
            params.append(f"%{term}%")

    if filters.get("has_attachments"):
        where.append("has_attachments = 1")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params


def build_message_query(filters: dict, order: str = "default") -> tuple[str, list]:
    """Row query for the unified browser: (sql, params). ``order`` is an
    allowlist of 'default' (newest first) or 'oldest' (oldest first)."""
    where_sql, params = _message_where(filters)
    order_sql = _BROWSER_ORDER_OLDEST if order == "oldest" else _BROWSER_ORDER_DEFAULT
    sql = (
        f"SELECT {MESSAGE_COLS} FROM messages {where_sql} "
        f"ORDER BY {order_sql} LIMIT {BROWSER_LIMIT}"
    )
    return sql, params


def build_message_count(filters: dict) -> tuple[str, list]:
    """Total matching rows (same WHERE as build_message_query, no LIMIT)."""
    where_sql, params = _message_where(filters)
    return f"SELECT COUNT(*) FROM messages {where_sql}", params


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
