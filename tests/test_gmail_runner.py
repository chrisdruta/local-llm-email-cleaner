"""Action runner: approved-only, reconcile-before-act, audit rows, never delete."""

from __future__ import annotations

import dataclasses
import inspect

from conftest import insert_message

from local_llm_email_cleaner.gmail import auth, reconcile, runner


class FakeRequest:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeMessagesApi:
    """Emulates service.users().messages() with a dict of live messages."""

    def __init__(self, live: dict[str, dict]):
        # live: rfc_message_id -> {"id": gmail_api_id, "headers": {...}}
        self.live = live
        self.trashed: list[str] = []
        self.modified: list[tuple[str, dict]] = []

    def list(self, userId, q, maxResults):
        rfc_id = q.removeprefix("rfc822msgid:")
        hit = self.live.get(rfc_id)
        return FakeRequest(lambda: {"messages": [{"id": hit["id"]}]} if hit else {})

    def get(self, userId, id, format, metadataHeaders):
        for entry in self.live.values():
            if entry["id"] == id:
                headers = [{"name": k, "value": v} for k, v in entry["headers"].items()]
                return FakeRequest(lambda: {"payload": {"headers": headers}})
        return FakeRequest(lambda: {"payload": {"headers": []}})

    def trash(self, userId, id):
        return FakeRequest(lambda: self.trashed.append(id) or {"id": id})

    def modify(self, userId, id, body):
        return FakeRequest(lambda: self.modified.append((id, body)) or {"id": id})


class FakeLabelsApi:
    """Emulates service.users().labels() with an in-memory label list."""

    def __init__(self, labels: list[dict] | None = None):
        self.labels = list(labels or [])
        self.created: list[dict] = []

    def list(self, userId):
        return FakeRequest(lambda: {"labels": list(self.labels)})

    def create(self, userId, body):
        def _create():
            label = {"id": f"Label_{len(self.labels) + 1}", "name": body["name"]}
            self.labels.append(label)
            self.created.append(label)
            return label

        return FakeRequest(_create)


class FakeService:
    def __init__(self, messages_api, labels_api=None):
        self._messages = messages_api
        self._labels = labels_api or FakeLabelsApi()

    def users(self):
        return self

    def messages(self):
        return self._messages

    def labels(self):
        return self._labels


def live_entry(
    rfc_id, gmail_id, from_addr="noreply@spam.example", subject="test message"
):
    return {
        rfc_id: {
            "id": gmail_id,
            "headers": {
                "Message-ID": f"<{rfc_id}>",
                "From": f"Sender <{from_addr}>",
                "Subject": subject,
            },
        }
    }


def approved_message(conn, rfc_id="m1@example.com", action="trash", status="approved"):
    return insert_message(
        conn,
        rfc_message_id=rfc_id,
        staged_label="DELETE_CANDIDATE",
        proposed_action=action,
        review_status=status,
        classified_by="rules+llm",
        ai_confidence=0.95,
    )


def get_status(conn, msg_id):
    return conn.execute(
        "SELECT review_status FROM messages WHERE id=?", (msg_id,)
    ).fetchone()[0]


def test_dry_run_reconciles_but_never_mutates(conn, cfg):
    msg_id = approved_message(conn)
    api = FakeMessagesApi(live_entry("m1@example.com", "G1"))

    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=False)
    assert stats.succeeded == 1
    assert api.trashed == [] and api.modified == []
    assert get_status(conn, msg_id) == "approved"  # untouched: a real run can still act

    audit = conn.execute(
        "SELECT * FROM actions WHERE message_id=?", (msg_id,)
    ).fetchone()
    assert audit["dry_run"] == 1
    assert audit["status"] == "success"
    assert audit["match_confirmed"] == 1
    assert audit["gmail_api_msgid"] == "G1"


def test_execute_trashes_confirmed_match(conn, cfg):
    msg_id = approved_message(conn)
    api = FakeMessagesApi(live_entry("m1@example.com", "G1"))

    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert stats.succeeded == 1
    assert api.trashed == ["G1"]
    assert get_status(conn, msg_id) == "applied"


def test_execute_archive_removes_inbox_and_adds_label(conn, cfg):
    approved_message(conn, action="archive")
    api = FakeMessagesApi(live_entry("m1@example.com", "G1"))
    labels = FakeLabelsApi()

    runner.apply_actions(conn, cfg, FakeService(api, labels), execute=True)
    assert labels.created == [{"id": "Label_1", "name": "EmailCleaner/Archived"}]
    assert api.modified == [
        ("G1", {"removeLabelIds": ["INBOX"], "addLabelIds": ["Label_1"]})
    ]
    assert api.trashed == []


def test_archive_reuses_existing_label(conn, cfg):
    approved_message(conn, rfc_id="m1@example.com", action="archive")
    approved_message(conn, rfc_id="m2@example.com", action="archive")
    live = live_entry("m1@example.com", "G1") | live_entry("m2@example.com", "G2")
    api = FakeMessagesApi(live)
    labels = FakeLabelsApi([{"id": "Label_7", "name": "emailcleaner/archived"}])

    runner.apply_actions(conn, cfg, FakeService(api, labels), execute=True)
    assert labels.created == []  # matched case-insensitively, resolved once
    assert [body["addLabelIds"] for _, body in api.modified] == [
        ["Label_7"],
        ["Label_7"],
    ]


def test_archive_label_disabled(conn, cfg):
    approved_message(conn, action="archive")
    api = FakeMessagesApi(live_entry("m1@example.com", "G1"))
    labels = FakeLabelsApi()
    cfg = dataclasses.replace(cfg, archive_label="")

    runner.apply_actions(conn, cfg, FakeService(api, labels), execute=True)
    assert api.modified == [("G1", {"removeLabelIds": ["INBOX"]})]
    assert labels.created == []


def test_dry_run_archive_never_creates_label(conn, cfg):
    approved_message(conn, action="archive")
    api = FakeMessagesApi(live_entry("m1@example.com", "G1"))
    labels = FakeLabelsApi()

    runner.apply_actions(conn, cfg, FakeService(api, labels), execute=False)
    assert labels.created == [] and api.modified == []


def test_only_approved_rows_are_consumed(conn, cfg):
    pending = insert_message(
        conn,
        rfc_message_id="p@example.com",
        proposed_action="trash",
        staged_label="DELETE_CANDIDATE",
        review_status="pending",
    )
    rejected = insert_message(
        conn,
        rfc_message_id="r@example.com",
        proposed_action="trash",
        staged_label="DELETE_CANDIDATE",
        review_status="rejected",
    )
    auto = approved_message(conn, rfc_id="a@example.com", status="auto_approved")

    api = FakeMessagesApi(live_entry("a@example.com", "G9"))
    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=True)

    assert stats.examined == 1
    assert api.trashed == ["G9"]
    assert get_status(conn, pending) == "pending"
    assert get_status(conn, rejected) == "rejected"
    assert get_status(conn, auto) == "applied"


def test_no_live_match_skips_and_never_acts(conn, cfg):
    msg_id = approved_message(conn)  # not present in the fake live mailbox
    api = FakeMessagesApi({})

    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert stats.skipped == 1 and stats.succeeded == 0
    assert api.trashed == []
    assert get_status(conn, msg_id) == "skipped"

    audit = conn.execute(
        "SELECT * FROM actions WHERE message_id=?", (msg_id,)
    ).fetchone()
    assert audit["status"] == "skipped"
    assert audit["match_confirmed"] == 0


def test_metadata_mismatch_skips(conn, cfg):
    msg_id = approved_message(conn)
    entry = live_entry("m1@example.com", "G1")
    entry["m1@example.com"]["headers"]["Message-ID"] = "<other@example.com>"
    api = FakeMessagesApi(entry)

    runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert api.trashed == []
    assert get_status(conn, msg_id) == "skipped"


def test_missing_message_id_skips(conn, cfg):
    msg_id = approved_message(conn, rfc_id=None)
    api = FakeMessagesApi({})

    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert stats.skipped == 1
    assert get_status(conn, msg_id) == "skipped"


def test_permanent_delete_never_used():
    """No code path in the Gmail layer may call a delete endpoint."""
    for module in (runner, reconcile, auth):
        assert ".delete(" not in inspect.getsource(module)
    # And the scope cannot permanently delete (delete needs mail.google.com).
    assert auth.SCOPES == ["https://www.googleapis.com/auth/gmail.modify"]
