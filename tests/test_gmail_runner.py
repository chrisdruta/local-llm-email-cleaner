"""Action runner: approved-only, reconcile-before-act, audit rows, never delete."""

from __future__ import annotations

import dataclasses
import inspect
import re
import socket
from email.utils import parseaddr

from conftest import insert_message

from local_llm_email_cleaner.gmail import auth, reconcile, runner


class FakeRequest:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeBatch:
    """Emulates googleapiclient's BatchHttpRequest: collected gets fire on
    execute(), each routed to the callback with (request_id, response, exc)."""

    def __init__(self, callback, log):
        self._callback = callback
        self._log = log
        self._reqs: list[tuple[str, FakeRequest]] = []

    def add(self, request, request_id):
        self._reqs.append((request_id, request))

    def execute(self):
        self._log.append(len(self._reqs))  # record sub-request count per batch
        for request_id, request in self._reqs:
            try:
                self._callback(request_id, request.execute(), None)
            except Exception as exc:  # noqa: BLE001
                self._callback(request_id, None, exc)


class FakeMessagesApi:
    """Emulates service.users().messages() with a dict of live messages."""

    def __init__(self, live: dict[str, dict]):
        # live: rfc_message_id -> {"id": gmail_api_id, "headers": {...}}
        self.live = live
        self.trashed: list[str] = []
        self.modified: list[tuple[str, dict]] = []
        self.batch_modified: list[dict] = []

    def list(self, userId, q, maxResults):
        if q.startswith("rfc822msgid:"):
            # The value is quoted to keep whitespace/operators literal.
            rfc_id = q.removeprefix("rfc822msgid:").strip('"')
            hit = self.live.get(rfc_id)
            return FakeRequest(lambda: {"messages": [{"id": hit["id"]}]} if hit else {})
        # Metadata fallback: from:"addr" subject:"subj" after:.. before:..
        want_from = (re.search(r'from:"([^"]*)"', q) or [None, None])[1]
        want_subj = (re.search(r'subject:"([^"]*)"', q) or [None, None])[1]
        matches = []
        for entry in self.live.values():
            _, addr = parseaddr(entry["headers"].get("From", ""))
            if want_from and addr.lower() != want_from.lower():
                continue
            if (
                want_subj is not None
                and entry["headers"].get("Subject", "") != want_subj
            ):
                continue
            matches.append({"id": entry["id"]})
        return FakeRequest(
            lambda: {"messages": matches[:maxResults]} if matches else {}
        )

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

    def batchModify(self, userId, body):
        return FakeRequest(lambda: self.batch_modified.append(body) or {})


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
        self.batches: list[int] = []  # sub-request count of each executed batch

    def users(self):
        return self

    def messages(self):
        return self._messages

    def labels(self):
        return self._labels

    def new_batch_http_request(self, callback):
        return FakeBatch(callback, self.batches)


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
        rule_action=action,
        staged_action=action,
        review_status=status,
        decision_source="rule+llm",
        llm_confidence=0.95,
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


def test_execute_archive_batches_inbox_removal_and_label(conn, cfg):
    msg_id = approved_message(conn, action="archive")
    api = FakeMessagesApi(live_entry("m1@example.com", "G1"))
    labels = FakeLabelsApi()

    runner.apply_actions(conn, cfg, FakeService(api, labels), execute=True)
    assert labels.created == [{"id": "Label_1", "name": "EmailCleaner/Archived"}]
    assert api.batch_modified == [
        {"ids": ["G1"], "removeLabelIds": ["INBOX"], "addLabelIds": ["Label_1"]}
    ]
    assert api.trashed == [] and api.modified == []
    assert get_status(conn, msg_id) == "applied"


def test_archive_reuses_existing_label(conn, cfg):
    approved_message(conn, rfc_id="m1@example.com", action="archive")
    approved_message(conn, rfc_id="m2@example.com", action="archive")
    live = live_entry("m1@example.com", "G1") | live_entry("m2@example.com", "G2")
    api = FakeMessagesApi(live)
    labels = FakeLabelsApi([{"id": "Label_7", "name": "emailcleaner/archived"}])

    runner.apply_actions(conn, cfg, FakeService(api, labels), execute=True)
    assert labels.created == []  # matched case-insensitively, resolved once
    # Both archives ride a single batchModify call with the shared label.
    assert api.batch_modified == [
        {"ids": ["G1", "G2"], "removeLabelIds": ["INBOX"], "addLabelIds": ["Label_7"]}
    ]


def test_archive_label_disabled(conn, cfg):
    approved_message(conn, action="archive")
    api = FakeMessagesApi(live_entry("m1@example.com", "G1"))
    labels = FakeLabelsApi()
    cfg = dataclasses.replace(cfg, archive_label="")

    runner.apply_actions(conn, cfg, FakeService(api, labels), execute=True)
    assert api.batch_modified == [{"ids": ["G1"], "removeLabelIds": ["INBOX"]}]
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
        staged_action="trash",
        rule_action="trash",
        review_status="pending",
    )
    rejected = insert_message(
        conn,
        rfc_message_id="r@example.com",
        staged_action="trash",
        rule_action="trash",
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


class FakeHttpError(Exception):
    """Stand-in built the way googleapiclient.errors.HttpError is consumed."""


def make_http_error(status=404):
    from googleapiclient.errors import HttpError

    class Resp(dict):
        def __init__(self, status):
            super().__init__({"status": str(status)})
            self.status = status
            self.reason = "fake"

    return HttpError(Resp(status), b"fake error")


def test_dry_run_unconfirmed_does_not_mutate_review_status(conn, cfg):
    """A dry run must never change decision state — not even to 'skipped'."""
    msg_id = approved_message(conn)  # absent from the fake live mailbox
    api = FakeMessagesApi({})

    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=False)
    assert stats.skipped == 1
    assert get_status(conn, msg_id) == "approved"  # still actionable later

    audit = conn.execute(
        "SELECT * FROM actions WHERE message_id=?", (msg_id,)
    ).fetchone()
    assert audit["status"] == "skipped" and audit["dry_run"] == 1

    # The same run with execute=True DOES take it out of the queue.
    runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert get_status(conn, msg_id) == "skipped"


def test_intent_row_written_before_success(conn, cfg):
    """Write-intent-then-mark-success: one row, attempt -> success."""
    msg_id = approved_message(conn)
    api = FakeMessagesApi(live_entry("m1@example.com", "G1"))

    seen_during_mutation: list[tuple] = []
    original_trash = api.trash

    def spying_trash(userId, id):
        # The committed intent row must already exist when Gmail is called.
        row = conn.execute(
            "SELECT status, completed_at FROM actions WHERE message_id=?", (msg_id,)
        ).fetchone()
        seen_during_mutation.append((row["status"], row["completed_at"]))
        return original_trash(userId=userId, id=id)

    api.trash = spying_trash
    runner.apply_actions(conn, cfg, FakeService(api), execute=True)

    assert seen_during_mutation == [("attempt", None)]
    audit = conn.execute(
        "SELECT * FROM actions WHERE message_id=?", (msg_id,)
    ).fetchall()
    assert len(audit) == 1  # the attempt row is finalized, not duplicated
    assert audit[0]["status"] == "success"
    assert audit[0]["completed_at"] is not None
    assert audit[0]["reconciled"] == 1


def test_reconcile_error_records_unreconciled(conn, cfg):
    msg_id = approved_message(conn)
    api = FakeMessagesApi(live_entry("m1@example.com", "G1"))

    def exploding_list(userId, q, maxResults):
        return FakeRequest(lambda: (_ for _ in ()).throw(make_http_error(404)))

    api.list = exploding_list
    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert stats.errors == 1

    audit = conn.execute(
        "SELECT * FROM actions WHERE message_id=?", (msg_id,)
    ).fetchone()
    assert audit["status"] == "error"
    assert audit["reconciled"] == 0  # reconcile never completed
    assert audit["match_confirmed"] == 0
    assert get_status(conn, msg_id) == "approved"  # untouched, re-runnable


def test_from_mismatch_skips(conn, cfg):
    msg_id = approved_message(conn)
    entry = live_entry("m1@example.com", "G1", from_addr="other-sender@evil.example")
    api = FakeMessagesApi(entry)

    runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert api.trashed == []
    assert get_status(conn, msg_id) == "skipped"


def test_substring_from_no_longer_confirms(conn, cfg):
    """'noreply@spam.example' inside a different live address must not match."""
    msg_id = approved_message(conn)
    entry = live_entry("m1@example.com", "G1", from_addr="xnoreply@spam.example.evil")
    api = FakeMessagesApi(entry)

    runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert api.trashed == []
    assert get_status(conn, msg_id) == "skipped"


def test_mixed_case_message_id_reconciles(conn, cfg):
    """The rfc822msgid: query must preserve the stored Message-ID's case."""
    msg_id = approved_message(conn, rfc_id="AbC123XyZ@Mail.Example")
    api = FakeMessagesApi(live_entry("AbC123XyZ@Mail.Example", "G1"))

    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert stats.succeeded == 1
    assert api.trashed == ["G1"]
    assert get_status(conn, msg_id) == "applied"


def test_failed_archive_batch_marks_chunk_error_and_stays_approved(conn, cfg):
    m1 = approved_message(conn, rfc_id="m1@example.com", action="archive")
    m2 = approved_message(conn, rfc_id="m2@example.com", action="archive")
    live = live_entry("m1@example.com", "G1") | live_entry("m2@example.com", "G2")
    api = FakeMessagesApi(live)

    def exploding_batch(userId, body):
        return FakeRequest(lambda: (_ for _ in ()).throw(make_http_error(404)))

    api.batchModify = exploding_batch
    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert stats.errors == 2 and stats.succeeded == 0

    for msg_id in (m1, m2):
        audit = conn.execute(
            "SELECT * FROM actions WHERE message_id=?", (msg_id,)
        ).fetchone()
        assert audit["status"] == "error"
        assert audit["completed_at"] is not None
        # review_status untouched -> the chunk is safely re-runnable.
        assert get_status(conn, msg_id) == "approved"


def test_archive_chunking_respects_batch_size(conn, cfg, monkeypatch):
    monkeypatch.setattr(runner, "ARCHIVE_BATCH_SIZE", 2)
    live = {}
    for i in range(1, 4):
        approved_message(conn, rfc_id=f"m{i}@example.com", action="archive")
        live |= live_entry(f"m{i}@example.com", f"G{i}")
    api = FakeMessagesApi(live)

    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert stats.succeeded == 3
    assert [b["ids"] for b in api.batch_modified] == [["G1", "G2"], ["G3"]]


def test_message_id_with_space_is_quoted_not_split(conn, cfg):
    """A malformed Message-ID containing a space must be searched as one literal
    value (quoted), not split into two ANDed terms."""
    weird = "foo bar@mail.example"
    msg_id = approved_message(conn, rfc_id=weird)
    api = FakeMessagesApi(live_entry(weird, "G1"))

    captured = {}
    original_list = api.list

    def spy_list(userId, q, maxResults):
        captured["q"] = q
        return original_list(userId=userId, q=q, maxResults=maxResults)

    api.list = spy_list
    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert captured["q"] == 'rfc822msgid:"foo bar@mail.example"'
    assert stats.succeeded == 1
    assert get_status(conn, msg_id) == "applied"


def test_metadata_fallback_reconciles_without_message_id(conn, cfg):
    """No RFC Message-ID -> reconcile by sender+subject+date and still act."""
    msg_id = approved_message(conn, rfc_id=None)  # default from/subject/date
    api = FakeMessagesApi(live_entry("any-key", "G5"))  # matches default From+Subject

    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert stats.succeeded == 1
    assert api.trashed == ["G5"]
    assert get_status(conn, msg_id) == "applied"

    audit = conn.execute(
        "SELECT * FROM actions WHERE message_id=?", (msg_id,)
    ).fetchone()
    assert audit["match_method"] == "metadata"
    assert audit["match_confirmed"] == 1


def test_metadata_fallback_ambiguous_skips(conn, cfg):
    """Two live messages match the metadata -> skip, never guess."""
    msg_id = approved_message(conn, rfc_id=None)
    live = live_entry("k1", "G1") | live_entry("k2", "G2")  # both match default
    api = FakeMessagesApi(live)

    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert stats.skipped == 1 and api.trashed == []
    assert get_status(conn, msg_id) == "skipped"


def test_metadata_fallback_subject_mismatch_skips(conn, cfg):
    """Single live hit whose subject differs is not confirmed (strict match)."""
    msg_id = approved_message(conn, rfc_id=None)
    entry = live_entry("k1", "G1")
    entry["k1"]["headers"]["Subject"] = "a totally different subject"
    api = FakeMessagesApi(entry)

    # The narrowed search wouldn't return it, but force the candidate through to
    # prove the post-fetch subject check is what rejects it.
    api.list = lambda userId, q, maxResults: FakeRequest(
        lambda: {"messages": [{"id": "G1"}]}
    )
    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert stats.skipped == 1 and api.trashed == []
    assert get_status(conn, msg_id) == "skipped"


def test_transient_network_error_is_retried_then_succeeds(conn, cfg, monkeypatch):
    """A transient (non-HttpError) failure backs off and retries, not aborts."""
    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    msg_id = approved_message(conn)
    api = FakeMessagesApi(live_entry("m1@example.com", "G1"))

    calls = {"n": 0}
    original_trash = api.trash

    def flaky_trash(userId, id):
        def _run():
            calls["n"] += 1
            if calls["n"] == 1:
                raise socket.timeout("connection timed out")
            return original_trash(userId=userId, id=id).execute()

        return FakeRequest(_run)

    api.trash = flaky_trash
    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert stats.succeeded == 1
    assert api.trashed == ["G1"]
    assert get_status(conn, msg_id) == "applied"


def test_persistent_transient_error_isolated_run_continues(conn, cfg, monkeypatch):
    """One message failing with a transient error after all retries records an
    error and does NOT abort the run; later messages still process."""
    monkeypatch.setattr(runner.time, "sleep", lambda _s: None)
    bad = approved_message(conn, rfc_id="bad@example.com")
    good = approved_message(conn, rfc_id="good@example.com")
    live = live_entry("bad@example.com", "GBAD") | live_entry(
        "good@example.com", "GOOD"
    )
    api = FakeMessagesApi(live)

    def trash(userId, id):
        if id == "GBAD":
            return FakeRequest(lambda: (_ for _ in ()).throw(ConnectionResetError()))
        return FakeRequest(lambda: api.trashed.append(id) or {"id": id})

    api.trash = trash
    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert stats.errors == 1 and stats.succeeded == 1
    assert api.trashed == ["GOOD"]
    assert get_status(conn, bad) == "approved"  # untouched -> re-runnable
    assert get_status(conn, good) == "applied"

    audit = conn.execute(
        "SELECT status, http_status FROM actions WHERE message_id=?", (bad,)
    ).fetchone()
    assert audit["status"] == "error"
    assert audit["http_status"] is None  # transient errors carry no HTTP status


def test_reconcile_batches_metadata_gets(conn, cfg):
    """The per-message metadata gets ride a single batched HTTP request."""
    live = {}
    for i in range(3):
        approved_message(conn, rfc_id=f"m{i}@example.com")
        live |= live_entry(f"m{i}@example.com", f"G{i}")
    api = FakeMessagesApi(live)
    svc = FakeService(api)

    stats = runner.apply_actions(conn, cfg, svc, execute=True)
    assert stats.succeeded == 3
    assert api.trashed == ["G0", "G1", "G2"]
    assert svc.batches == [3]  # one batch carrying all three metadata gets


def test_reconcile_chunks_respect_batch_limit(conn, cfg, monkeypatch):
    monkeypatch.setattr(runner, "RECONCILE_BATCH_SIZE", 2)
    live = {}
    for i in range(3):
        approved_message(conn, rfc_id=f"m{i}@example.com")
        live |= live_entry(f"m{i}@example.com", f"G{i}")
    svc = FakeService(FakeMessagesApi(live))

    stats = runner.apply_actions(conn, cfg, svc, execute=True)
    assert stats.succeeded == 3
    assert svc.batches == [2, 1]  # two reconcile chunks -> two batched gets


def test_batched_get_failure_records_error_not_skip(conn, cfg):
    """A metadata get that fails inside the batch must record a re-runnable
    error (review_status stays approved), never a silent skip."""
    msg_id = approved_message(conn)
    api = FakeMessagesApi(live_entry("m1@example.com", "G1"))

    def exploding_get(userId, id, format, metadataHeaders):
        return FakeRequest(lambda: (_ for _ in ()).throw(make_http_error(500)))

    api.get = exploding_get
    stats = runner.apply_actions(conn, cfg, FakeService(api), execute=True)
    assert stats.errors == 1 and api.trashed == []
    assert get_status(conn, msg_id) == "approved"  # re-runnable, not skipped

    audit = conn.execute(
        "SELECT status, reconciled FROM actions WHERE message_id=?", (msg_id,)
    ).fetchone()
    assert audit["status"] == "error" and audit["reconciled"] == 0


def test_permanent_delete_never_used():
    """No code path in the Gmail layer may call a delete endpoint."""
    for module in (runner, reconcile, auth):
        assert ".delete(" not in inspect.getsource(module)
    # And the scope cannot permanently delete (delete needs mail.google.com).
    assert auth.SCOPES == ["https://www.googleapis.com/auth/gmail.modify"]
