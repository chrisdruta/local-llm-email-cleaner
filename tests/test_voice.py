"""Google Voice SMS / call-log parsing, export, and trash disposition."""

from __future__ import annotations

import csv
import json
import mailbox
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime

from conftest import USER_ADDR, insert_message

from local_llm_email_cleaner.ingest import voice
from local_llm_email_cleaner.llm import classifier
from local_llm_email_cleaner.models import CLASSIFIED_BY_RULES, CLASSIFIED_BY_VOICE
from local_llm_email_cleaner.voice_export import VOICE_EXPORT_RULE, export_voice

PNG = b"\x89PNG\r\n\x1a\n fake image bytes"


def make_mms_mbox(
    tmp_path, *, message_id="mms-1@example.com", name="img.png", data=PNG
):
    path = tmp_path / "mms.mbox"
    box = mailbox.mbox(str(path))
    msg = EmailMessage()
    msg["From"] = "+12164969651@unknown.email"
    msg["To"] = USER_ADDR
    msg["Subject"] = "SMS with Michael"
    msg["Message-ID"] = f"<{message_id}>"
    msg["Date"] = format_datetime(datetime(2019, 4, 1, 10, 0, tzinfo=UTC))
    msg["X-Gmail-Labels"] = "SMS"
    msg.set_content("here is a pic")
    msg.add_attachment(data, maintype="image", subtype="png", filename=name)
    box.add(msg)
    box.close()
    return path


def add_mms_row(conn, *, message_id="mms-1@example.com", name="img.png"):
    return insert_message(
        conn,
        from_addr="+12164969651@unknown.email",
        from_name="Michael",
        subject="SMS with Michael",
        body_text="here is a pic",
        labels="SMS",
        rfc_message_id=message_id,
        has_attachments=1,
        attachment_names=json.dumps([name]),
    )


def _row(conn, msg_id):
    return conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()


def add_inbound_sms(
    conn, *, name="Michael Redacted", number="+12164969651", body="hey"
):
    mid = insert_message(
        conn,
        from_addr=f"{number}@unknown.email",
        from_domain="unknown.email",
        from_name=name,
        subject=f"SMS with {name}",
        body_text=body,
        labels="SMS",
    )
    return voice.parse_message(_row(conn, mid))


def add_outbound_sms(conn, *, name="Michael Redacted", body="hi back"):
    mid = insert_message(
        conn,
        from_addr=USER_ADDR,
        from_domain="example.com",
        from_name=None,
        subject=f"SMS with {name}",
        body_text=body,
        labels="SMS",
    )
    return voice.parse_message(_row(conn, mid))


def add_call(conn, *, number="4408795640", call_type="incoming", secs="00:00:23"):
    mid = insert_message(
        conn,
        from_addr=f"{number}@unknown.email",
        from_domain="unknown.email",
        from_name=number,
        subject=f"Call with {number}",
        body_text=f"23s ({secs})\n{number} ({call_type} call)",
        labels="Call log",
    )
    return voice.parse_message(_row(conn, mid))


# --- parsing ----------------------------------------------------------------


def test_parse_inbound_sms(conn):
    m = add_inbound_sms(conn)
    assert m.kind == voice.KIND_SMS
    assert m.direction == voice.DIRECTION_INBOUND
    assert m.contact_name == "Michael Redacted"
    assert m.contact_number == "+12164969651"
    assert m.text == "hey"
    assert m.contact_key == "michael redacted"


def test_parse_outbound_sms_groups_with_inbound(conn):
    inbound = add_inbound_sms(conn)
    outbound = add_outbound_sms(conn)
    assert outbound.direction == voice.DIRECTION_OUTBOUND
    # Same subject contact -> same grouping key as the inbound side.
    assert outbound.contact_key == inbound.contact_key


def test_parse_call_directions_and_duration(conn):
    incoming = add_call(conn, call_type="incoming", secs="00:00:23")
    assert incoming.direction == voice.DIRECTION_INBOUND
    assert incoming.duration_seconds == 23
    assert incoming.call_type == "incoming"
    assert incoming.contact_number == "+14408795640"  # 10-digit NANP -> +1
    assert incoming.contact_name is None  # numeric from_name is not a name

    outgoing = add_call(
        conn, number="4405203481", call_type="outgoing", secs="00:01:05"
    )
    assert outgoing.direction == voice.DIRECTION_OUTBOUND
    assert outgoing.duration_seconds == 65

    missed = add_call(conn, number="4405203481", call_type="missed", secs="00:00:00")
    assert missed.direction == voice.DIRECTION_MISSED
    assert missed.duration_seconds == 0


def test_parse_malformed_call_body_does_not_crash(conn):
    mid = insert_message(
        conn,
        from_addr="5551234@unknown.email",
        subject="Call with 5551234",
        body_text="this body has no structure at all",
        labels="Call log",
    )
    m = voice.parse_message(_row(conn, mid))
    assert m.kind == voice.KIND_CALL
    assert m.duration_seconds is None
    assert m.call_type is None
    assert m.direction == voice.DIRECTION_UNKNOWN


def test_non_voice_message_is_ignored(conn):
    mid = insert_message(conn, labels="Inbox", subject="a normal email")
    assert voice.parse_message(_row(conn, mid)) is None


def test_normalize_number():
    assert voice.normalize_number("4408795640") == "+14408795640"
    assert voice.normalize_number("+12164969651") == "+12164969651"
    assert voice.normalize_number("14408795640") == "+14408795640"
    assert voice.normalize_number("50409") == "50409"  # short code untouched
    assert voice.normalize_number("1410200646") == "1410200646"  # not NANP, untouched
    assert voice.normalize_number(None) is None


# --- export -----------------------------------------------------------------


def test_export_writes_jsonl_csv_and_transcripts(conn, tmp_path):
    add_inbound_sms(conn, body="first")
    add_outbound_sms(conn, body="second")
    add_call(conn)

    stats = export_voice(conn, tmp_path, set_disposition=False)
    assert stats.sms == 2
    assert stats.calls == 1
    assert stats.contacts == 1

    sms_lines = (tmp_path / "sms.jsonl").read_text().splitlines()
    assert len(sms_lines) == 2
    rec = json.loads(sms_lines[0])
    assert rec["type"] == "sms"
    assert rec["contact"]["number"] == "+12164969651"

    assert len(list((tmp_path / "calls.jsonl").read_text().splitlines())) == 1
    call_rec = json.loads((tmp_path / "calls.jsonl").read_text())
    assert call_rec["duration_seconds"] == 23

    md_files = list((tmp_path / "sms").glob("*.md"))
    assert len(md_files) == 1
    transcript = md_files[0].read_text()
    assert "first" in transcript and "second" in transcript
    assert "Me:" in transcript  # outbound speaker label

    csv_rows = list(csv.DictReader((tmp_path / "calls.csv").open()))
    assert csv_rows[0]["call_type"] == "incoming"


def test_export_csv_neutralizes_formula_injection(conn, tmp_path):
    insert_message(
        conn,
        from_addr="4408795640@unknown.email",
        from_name="=cmd|calc",
        subject="Call with 4408795640",
        body_text="23s (00:00:23)\n4408795640 (incoming call)",
        labels="Call log",
    )
    export_voice(conn, tmp_path, set_disposition=False)
    row = next(csv.DictReader((tmp_path / "calls.csv").open()))
    assert row["contact_name"].startswith("'=")
    assert row["contact_number"].startswith("'+")  # '+1...' is a formula char too


def test_export_is_idempotent(conn, tmp_path):
    add_inbound_sms(conn)
    add_outbound_sms(conn)
    export_voice(conn, tmp_path)
    export_voice(conn, tmp_path)

    assert len((tmp_path / "sms.jsonl").read_text().splitlines()) == 2
    n_hits = conn.execute(
        "SELECT COUNT(*) FROM rule_hits WHERE rule_name=?", (VOICE_EXPORT_RULE,)
    ).fetchone()[0]
    assert n_hits == 2  # one per message, not duplicated


# --- disposition ------------------------------------------------------------


def test_export_stages_messages_for_trash(conn, tmp_path):
    m = add_inbound_sms(conn)
    export_voice(conn, tmp_path, set_disposition=True)

    row = _row(conn, m.message_id)
    assert row["staged_label"] == "DELETE_CANDIDATE"
    assert row["proposed_action"] == "trash"
    assert row["classified_by"] == CLASSIFIED_BY_VOICE
    assert row["ai_category"] == "voice_sms"
    assert row["review_status"] == "pending"  # still requires human approval

    hit = conn.execute(
        "SELECT rule_kind, outcome FROM rule_hits WHERE message_id=? AND rule_name=?",
        (row["id"], VOICE_EXPORT_RULE),
    ).fetchone()
    assert hit["rule_kind"] == "candidate"
    assert hit["outcome"] == "DELETE_CANDIDATE"


def test_disposition_skips_non_pending(conn, tmp_path):
    mid = insert_message(
        conn,
        from_addr="+12164969651@unknown.email",
        from_name="Michael",
        subject="SMS with Michael",
        body_text="hi",
        labels="SMS",
        review_status="approved",
        staged_label="KEEP",
    )
    export_voice(conn, tmp_path, set_disposition=True)
    row = _row(conn, mid)
    assert row["review_status"] == "approved"
    assert row["staged_label"] == "KEEP"  # an existing decision is never overwritten
    assert row["classified_by"] != CLASSIFIED_BY_VOICE


def test_no_trash_flag_leaves_messages_untouched(conn, tmp_path):
    m = add_inbound_sms(conn)
    export_voice(conn, tmp_path, set_disposition=False)
    row = _row(conn, m.message_id)
    assert row["classified_by"] != CLASSIFIED_BY_VOICE
    assert row["staged_label"] is None


def test_classifier_skips_voice_delete_candidates(conn, tmp_path):
    voice_msg = add_inbound_sms(conn)
    export_voice(conn, tmp_path, set_disposition=True)

    # A normal rule-staged delete candidate, by contrast, must still be selected.
    rule_msg = insert_message(
        conn,
        rfc_message_id="rule@x.com",
        staged_label="DELETE_CANDIDATE",
        proposed_action="trash",
        classified_by=CLASSIFIED_BY_RULES,
    )

    selected = {
        r["id"]
        for r in conn.execute(
            classifier._SELECT_SQL,
            {
                "needs_review": "NEEDS_REVIEW",
                "delete_candidate": "DELETE_CANDIDATE",
                "archive_candidate": "ARCHIVE_CANDIDATE",
                "voice": CLASSIFIED_BY_VOICE,
            },
        )
    }
    assert voice_msg.message_id not in selected  # backed up; never sent to the LLM
    assert rule_msg in selected


# --- attachments ------------------------------------------------------------


def test_attachments_recovered_from_mbox(conn, tmp_path):
    add_mms_row(conn)
    mbox = make_mms_mbox(tmp_path)

    out = tmp_path / "out"
    stats = export_voice(conn, out, set_disposition=False, mbox_path=mbox)
    assert stats.attachments_saved == 1
    assert stats.attachments_skipped == 0

    saved = list((out / "attachments").rglob("*.png"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == PNG

    rec = json.loads((out / "sms.jsonl").read_text().splitlines()[0])
    assert len(rec["attachments"]) == 1
    att = rec["attachments"][0]
    assert att["filename"] == "img.png"
    assert att["content_type"] == "image/png"
    assert att["size"] == len(PNG)
    assert att["path"].startswith("attachments/")

    transcript = next((out / "sms").glob("*.md")).read_text()
    assert "📎 img.png" in transcript


def test_attachments_missing_mbox_degrades_gracefully(conn, tmp_path):
    add_mms_row(conn)
    out = tmp_path / "out"
    stats = export_voice(
        conn, out, set_disposition=False, mbox_path=tmp_path / "nonexistent.mbox"
    )
    assert stats.attachments_saved == 0
    assert stats.attachments_skipped == 1
    assert not (out / "attachments").exists()

    rec = json.loads((out / "sms.jsonl").read_text().splitlines()[0])
    assert rec["attachments"] == [{"filename": "img.png", "exported": False}]


def test_no_attachments_flag_skips_recovery(conn, tmp_path):
    add_mms_row(conn)
    mbox = make_mms_mbox(tmp_path)
    out = tmp_path / "out"
    stats = export_voice(
        conn, out, set_disposition=False, mbox_path=mbox, include_attachments=False
    )
    assert stats.attachments_saved == 0
    assert not (out / "attachments").exists()


def test_extract_attachments_unit():
    from local_llm_email_cleaner.ingest import bodies

    msg = EmailMessage()
    msg.set_content("body")
    msg.add_attachment(PNG, maintype="image", subtype="png", filename="x.png")
    atts = bodies.extract_attachments(msg)
    assert len(atts) == 1
    name, ctype, data = atts[0]
    assert name == "x.png"
    assert ctype == "image/png"
    assert data == PNG
