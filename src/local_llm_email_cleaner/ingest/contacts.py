"""Known-contact derivation: people the user has SENT mail to."""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter

from .headers import addr_domain

logger = logging.getLogger(__name__)


def derive_contacts(conn: sqlite3.Connection, user_addresses: tuple[str, ...]) -> int:
    """Populate the contacts table from Sent messages in the ingested corpus.

    A message is Sent mail when its From address is one of the user's own
    addresses; every To/Cc recipient becomes a known contact.
    """
    if not user_addresses:
        logger.warning("No user_addresses configured; skipping contact derivation")
        return 0

    placeholders = ",".join("?" for _ in user_addresses)
    rows = conn.execute(
        f"SELECT to_all FROM messages WHERE from_addr IN ({placeholders}) AND to_all IS NOT NULL",
        tuple(user_addresses),
    )

    counts: Counter[str] = Counter()
    own = set(user_addresses)
    for (to_all,) in rows:
        for addr in to_all.split(","):
            addr = addr.strip()
            if addr and addr not in own:
                counts[addr] += 1

    conn.executemany(
        """
        INSERT INTO contacts (address, domain, sent_count) VALUES (?, ?, ?)
        ON CONFLICT(address) DO UPDATE SET sent_count = excluded.sent_count
        """,
        [(addr, addr_domain(addr), n) for addr, n in counts.items()],
    )
    conn.commit()
    return len(counts)


def suggest_user_addresses(
    conn: sqlite3.Connection, top_n: int = 5
) -> list[tuple[str, int]]:
    """Most frequent From addresses — a hint for configuring user_addresses."""
    rows = conn.execute(
        """
        SELECT from_addr, COUNT(*) AS n FROM messages
        WHERE from_addr IS NOT NULL
        GROUP BY from_addr ORDER BY n DESC LIMIT ?
        """,
        (top_n,),
    )
    return [(r["from_addr"], r["n"]) for r in rows]
