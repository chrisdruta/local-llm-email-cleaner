"""Known-contact derivation: people the user has SENT mail to."""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from email.utils import parseaddr

from .headers import addr_domain, normalize_addr
from .voice import UNKNOWN_DOMAIN

logger = logging.getLogger(__name__)


def derive_contacts(conn: sqlite3.Connection, user_addresses: tuple[str, ...]) -> int:
    """Populate the contacts table from Sent messages in the ingested corpus.

    A message is Sent mail when its From address is one of the user's own
    addresses; every To/Cc recipient becomes a known contact. Configured
    addresses are normalized the same way ingest normalized from_addr, so a
    display form ("Me <me@x.com>") or mixed case still matches.
    """
    if not user_addresses:
        logger.warning("No user_addresses configured; skipping contact derivation")
        return 0

    own = {a for addr in user_addresses if (a := normalize_addr(parseaddr(addr)[1]))}
    if not own:
        logger.warning(
            "None of the configured user_addresses look like email addresses; "
            "skipping contact derivation"
        )
        return 0

    placeholders = ",".join("?" for _ in own)
    rows = conn.execute(
        f"SELECT to_all FROM messages WHERE from_addr IN ({placeholders}) AND to_all IS NOT NULL",
        tuple(own),
    )

    counts: Counter[str] = Counter()
    for (to_all,) in rows:
        for addr in to_all.split(","):
            addr = addr.strip()
            if not addr or addr in own:
                continue
            # `<number>@unknown.email` is the Google Voice converter's synthetic
            # placeholder for the other party in an outbound SMS — not a real
            # address you correspond with. Deriving it as a contact would let
            # known_contact protect the matching inbound Voice records from the
            # voice cleanup rule (and inflate the contact list with phone numbers).
            if addr_domain(addr) == UNKNOWN_DOMAIN:
                continue
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
