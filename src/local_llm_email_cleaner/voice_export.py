"""Export Google Voice SMS / call-log messages to disk, then stage them for
trash.

Layout written under ``out_dir``:

    sms.jsonl              one normalized JSON record per SMS (lossless, re-importable)
    calls.jsonl            one record per call
    voicemails.jsonl       written only if any voicemail messages exist
    calls.csv              flat spreadsheet view of the calls
    sms/<contact>.md       human-readable per-contact transcript
    attachments/<contact>/ recovered MMS images etc.

Each line in the ``.jsonl`` files is self-describing:

    {"type":"sms","direction":"inbound","timestamp":"2019-...","contact":
     {"name":"Michael","number":"+12164969651"},"text":"...",
     "attachments":[{"filename":"img.jpg","path":"attachments/Michael/...",
     "content_type":"image/jpeg","size":12345}],"source":{...}}

Attachment *bytes* are discarded at ingest (only the filenames are kept), so to
preserve MMS pictures this re-reads the source mbox (located via
``meta.mbox_source``, overridable) and matches messages by ``rfc_message_id``.
If the mbox isn't reachable the text still exports and each affected record is
flagged ``"exported": false`` rather than failing.

After a successful write, exported messages are staged DELETE_CANDIDATE /
``trash`` (still ``pending`` — they go through normal review/approval before
anything is trashed) and tagged ``classified_by='voice'`` so the LLM
classifier skips them. The on-disk files are rewritten in full on every run,
so re-running is idempotent.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .export import _sanitize_cell
from .ingest import voice
from .ingest.mbox_reader import iter_attachments
from .ingest.voice import VoiceMessage
from .models import (
    CLASSIFIED_BY_VOICE,
    ProposedAction,
    RuleKind,
    StagedLabel,
)

logger = logging.getLogger(__name__)

VOICE_EXPORT_RULE = "voice_export"

_SELECT_SQL = """
SELECT id, rfc_message_id, thread_id, labels, date_utc, date_epoch,
       from_addr, from_name, subject, body_text, has_attachments, attachment_names
FROM messages
WHERE labels IS NOT NULL
  AND (labels LIKE '%SMS%' OR labels LIKE '%Call log%' OR labels LIKE '%Voicemail%')
ORDER BY id
"""

_CALL_CSV_FIELDS = (
    "timestamp",
    "direction",
    "call_type",
    "duration_seconds",
    "contact_name",
    "contact_number",
)

_CHUNK = 500  # keep SQL IN(...) under SQLite's variable limit

#: contact_key -> (filename stem, display name, number)
StemMap = dict[str, tuple[str, "str | None", "str | None"]]
#: messages.id -> list of attachment record dicts
Recovered = dict[int, list[dict]]


@dataclass
class VoiceExportStats:
    sms: int = 0
    calls: int = 0
    voicemails: int = 0
    contacts: int = 0
    staged_for_trash: int = 0
    attachments_saved: int = 0
    attachments_skipped: int = (
        0  # messages w/ attachments whose bytes weren't recovered
    )
    mbox_path: str = ""
    out_dir: str = ""
    files: list[str] = field(default_factory=list)


def export_voice(
    conn: sqlite3.Connection,
    out_dir: Path | str,
    *,
    set_disposition: bool = True,
    include_attachments: bool = True,
    mbox_path: Path | str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> VoiceExportStats:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    rows = conn.execute(_SELECT_SQL).fetchall()
    messages = [m for row in rows if (m := voice.parse_message(row)) is not None]

    sms = [m for m in messages if m.kind == voice.KIND_SMS]
    calls = [m for m in messages if m.kind == voice.KIND_CALL]
    voicemails = [m for m in messages if m.kind == voice.KIND_VOICEMAIL]

    stats = VoiceExportStats(
        sms=len(sms), calls=len(calls), voicemails=len(voicemails), out_dir=str(out)
    )

    # Contact stems anchor both the transcript filenames and the attachment
    # folders, so an image lands next to the conversation it belongs to.
    groups, stems = _assign_stems(sms + voicemails)

    recovered: Recovered = {}
    if include_attachments:
        resolved = Path(mbox_path) if mbox_path is not None else _resolve_mbox(conn)
        stats.mbox_path = str(resolved) if resolved else ""
        recovered = _export_attachments(out, messages, stems, resolved, stats, progress)

    _write_jsonl(out / "sms.jsonl", sms, recovered, stats)
    _write_jsonl(out / "calls.jsonl", calls, recovered, stats)
    if voicemails:
        _write_jsonl(out / "voicemails.jsonl", voicemails, recovered, stats)
    _write_calls_csv(out / "calls.csv", calls, stats)
    stats.contacts = _write_transcripts(
        out / "sms", sms, groups, stems, recovered, stats
    )

    if set_disposition and messages:
        stats.staged_for_trash = _stage_for_trash(conn, messages)

    logger.info(
        "Voice export: %d SMS, %d calls, %d voicemails across %d contacts -> %s "
        "(%d attachments saved, %d skipped, %d staged for trash)",
        stats.sms,
        stats.calls,
        stats.voicemails,
        stats.contacts,
        out,
        stats.attachments_saved,
        stats.attachments_skipped,
        stats.staged_for_trash,
    )
    return stats


# --- attachments ------------------------------------------------------------


def _resolve_mbox(conn: sqlite3.Connection) -> Path | None:
    row = conn.execute("SELECT value FROM meta WHERE key='mbox_source'").fetchone()
    return Path(row[0]) if row and row[0] else None


def _date_prefix(m: VoiceMessage) -> str:
    if not m.timestamp:
        return "nodate"
    return re.sub(r"\D", "", m.timestamp[:19]) or "nodate"


def _attachment_filename(m: VoiceMessage, idx: int, name: str) -> str:
    """Collision-proof, sortable on-disk name: date + db id + original name."""
    suffix = f"_{idx}" if idx else ""
    return f"{_date_prefix(m)}_id{m.message_id}{suffix}_{_safe_filename(name)}"


def _export_attachments(
    out: Path,
    messages: list[VoiceMessage],
    stems: StemMap,
    mbox_path: Path | None,
    stats: VoiceExportStats,
    progress: Callable[[int, int], None] | None = None,
) -> Recovered:
    wanted = {
        m.rfc_message_id: m for m in messages if m.has_attachments and m.rfc_message_id
    }
    expected = sum(1 for m in messages if m.has_attachments)
    if expected == 0:
        return {}
    if mbox_path is None or not mbox_path.is_file():
        logger.warning(
            "Source mbox unavailable (%s) — %d messages with attachments exported "
            "as text only; pass --mbox to recover the images",
            mbox_path,
            expected,
        )
        stats.attachments_skipped = expected
        return {}

    recovered: Recovered = {}
    for mid, atts in iter_attachments(mbox_path, set(wanted), on_scan=progress):
        m = wanted.get(mid)
        if m is None or not atts:
            continue
        stem = stems.get(m.contact_key, (None,))[0] or _safe_filename(
            m.contact_name or m.contact_number or m.contact_key
        )
        dest_dir = out / "attachments" / stem
        dest_dir.mkdir(parents=True, exist_ok=True)
        entries: list[dict] = []
        for idx, (name, ctype, data) in enumerate(atts):
            fname = _attachment_filename(m, idx, name)
            (dest_dir / fname).write_bytes(data)
            entries.append(
                {
                    "filename": name,
                    "content_type": ctype,
                    "size": len(data),
                    "path": f"attachments/{stem}/{fname}",
                }
            )
        if entries:
            recovered[m.message_id] = entries

    stats.attachments_saved = sum(len(v) for v in recovered.values())
    # A message is "skipped" if it had attachments but none were recovered
    # (no Message-ID, or not found in the mbox).
    stats.attachments_skipped = expected - len(recovered)
    return recovered


def _attachment_entries(m: VoiceMessage, recovered: Recovered) -> list[dict] | None:
    """Attachment records for a message: recovered bytes if we have them, else a
    filename-only stub flagged not-exported, else None when there are none."""
    if m.message_id in recovered:
        return recovered[m.message_id]
    if m.has_attachments:
        names = m.attachment_names or (None,)
        return [{"filename": n, "exported": False} for n in names]
    return None


# --- record serialization ---------------------------------------------------


def _record(m: VoiceMessage, recovered: Recovered) -> dict:
    rec: dict = {
        "type": m.kind,
        "direction": m.direction,
        "timestamp": m.timestamp,
        "contact": {"name": m.contact_name, "number": m.contact_number},
        "source": {
            "db_id": m.message_id,
            "rfc_message_id": m.rfc_message_id,
            "thread_id": m.thread_id,
        },
    }
    if m.kind == voice.KIND_CALL:
        rec["duration_seconds"] = m.duration_seconds
        rec["call_type"] = m.call_type
    else:
        rec["text"] = m.text
        if m.kind == voice.KIND_VOICEMAIL:
            rec["duration_seconds"] = m.duration_seconds
    attachments = _attachment_entries(m, recovered)
    if attachments is not None:
        rec["attachments"] = attachments
    return rec


def _sort_key(m: VoiceMessage) -> tuple:
    return (m.contact_key, m.epoch if m.epoch is not None else 0, m.message_id)


def _write_jsonl(
    path: Path, msgs: list[VoiceMessage], recovered: Recovered, stats: VoiceExportStats
) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for m in sorted(msgs, key=_sort_key):
            fh.write(json.dumps(_record(m, recovered), ensure_ascii=False) + "\n")
    stats.files.append(path.name)


def _write_calls_csv(
    path: Path, calls: list[VoiceMessage], stats: VoiceExportStats
) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_CALL_CSV_FIELDS)
        for m in sorted(calls, key=_sort_key):
            writer.writerow(
                [
                    m.timestamp,
                    m.direction,
                    m.call_type,
                    m.duration_seconds,
                    _sanitize_cell(m.contact_name),
                    _sanitize_cell(m.contact_number),
                ]
            )
    stats.files.append(path.name)


# --- per-contact transcripts ------------------------------------------------


def _safe_filename(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._+-]+", "-", name).strip("-")
    return slug or "unknown"


def _contact_display(group: list[VoiceMessage]) -> tuple[str, str | None, str | None]:
    """(filename stem, display name, number) for a contact's messages."""
    name = next((m.contact_name for m in group if m.contact_name), None)
    number = next((m.contact_number for m in group if m.contact_number), None)
    if name and number:
        stem = f"{_safe_filename(name)}_{_safe_filename(number)}"
    else:
        stem = _safe_filename(name or number or group[0].contact_key)
    return stem, name, number


def _assign_stems(
    msgs: list[VoiceMessage],
) -> tuple[dict[str, list[VoiceMessage]], StemMap]:
    """Group by contact and assign each a unique, filesystem-safe stem (shared
    by the transcript file and the attachments folder)."""
    groups: dict[str, list[VoiceMessage]] = {}
    for m in msgs:
        groups.setdefault(m.contact_key, []).append(m)

    used: set[str] = set()
    stems: StemMap = {}
    for key, group in groups.items():
        group.sort(key=lambda m: (m.epoch if m.epoch is not None else 0, m.message_id))
        stem, name, number = _contact_display(group)
        unique = stem
        n = 2
        while unique in used:
            unique = f"{stem}-{n}"
            n += 1
        used.add(unique)
        stems[key] = (unique, name, number)
    return groups, stems


def _write_transcripts(
    sms_dir: Path,
    sms: list[VoiceMessage],
    groups: dict[str, list[VoiceMessage]],
    stems: StemMap,
    recovered: Recovered,
    stats: VoiceExportStats,
) -> int:
    if not sms:
        return 0
    sms_dir.mkdir(parents=True, exist_ok=True)
    sms_keys = {m.contact_key for m in sms}
    written = 0
    for key in sms_keys:
        group = [m for m in groups[key] if m.kind == voice.KIND_SMS]
        stem, name, number = stems[key]
        header = name or number or key
        title = f"SMS with {header}" + (f" ({number})" if name and number else "")
        lines = [f"# {title}", ""]
        for m in group:
            who = (
                "Me"
                if m.direction == voice.DIRECTION_OUTBOUND
                else (name or number or header)
            )
            stamp = m.timestamp or "(no date)"
            lines.append(f"**{stamp} — {who}:**")
            text = (m.text or "").strip()
            if text:
                lines.append(text)
            for entry in _attachment_entries(m, recovered) or []:
                if entry.get("path"):
                    lines.append(f"📎 {entry['filename']} → {entry['path']}")
                else:
                    fname = entry.get("filename") or "attachment"
                    lines.append(f"📎 {fname} (not exported — source mbox unavailable)")
            lines.append("")
        (sms_dir / f"{stem}.md").write_text("\n".join(lines), encoding="utf-8")
        written += 1
    return written


# --- disposition ------------------------------------------------------------

_DISPOSITION_SQL = f"""
UPDATE messages
SET staged_label='{StagedLabel.DELETE_CANDIDATE.value}',
    proposed_action='{ProposedAction.TRASH.value}',
    ai_category=?,
    classified_by='{CLASSIFIED_BY_VOICE}'
WHERE id=? AND review_status='pending'
"""


def _stage_for_trash(conn: sqlite3.Connection, messages: list[VoiceMessage]) -> int:
    """Stage exported messages DELETE_CANDIDATE/trash (pending rows only, so an
    already-approved or applied decision is never overwritten) and record a
    'voice_export' candidate rule_hit for the audit trail. Idempotent."""
    by_id = {m.message_id: m for m in messages}
    ids = list(by_id)
    staged = 0
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        pending = [
            r[0]
            for r in conn.execute(
                f"SELECT id FROM messages WHERE id IN ({placeholders}) "
                "AND review_status='pending'",
                chunk,
            )
        ]
        if not pending:
            continue
        conn.executemany(
            _DISPOSITION_SQL,
            [(f"voice_{by_id[i].kind}", i) for i in pending],
        )
        # Refresh the rule_hit so re-runs don't accumulate duplicates.
        pend_ph = ",".join("?" for _ in pending)
        conn.execute(
            f"DELETE FROM rule_hits WHERE rule_name='{VOICE_EXPORT_RULE}' "
            f"AND message_id IN ({pend_ph})",
            pending,
        )
        conn.executemany(
            "INSERT INTO rule_hits (message_id, rule_name, rule_kind, outcome) "
            "VALUES (?, ?, ?, ?)",
            [
                (
                    i,
                    VOICE_EXPORT_RULE,
                    RuleKind.CANDIDATE.value,
                    StagedLabel.DELETE_CANDIDATE.value,
                )
                for i in pending
            ],
        )
        staged += len(pending)
    conn.commit()
    return staged
