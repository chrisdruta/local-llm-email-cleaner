-- Canonical schema for the email-cleaner SQLite database.
-- Executed idempotently by db.init_db(); bump schema_version on changes.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

-- Free-form key/value metadata (mbox source path, ingest timestamps, ...).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per MBOX message. SQLite treats NULLs as distinct in UNIQUE
-- constraints, so de-duplication keys off gmail_msgid / rfc_message_id when
-- present and falls back to dedup_key (a content hash) for the rare message
-- that carries neither identifier, keeping re-ingest idempotent for those too.
CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY,
    gmail_msgid       TEXT,                            -- X-GM-MSGID (often absent in Takeout)
    thread_id         TEXT,                            -- X-GM-THRID
    rfc_message_id    TEXT,                            -- RFC 822 Message-ID
    dedup_key         TEXT,                            -- content hash; set only when both ids are absent
    labels            TEXT,                            -- raw X-Gmail-Labels
    date_utc          TEXT,                            -- ISO-8601 UTC
    date_epoch        INTEGER,                         -- unix seconds, for age filters
    from_addr         TEXT,                            -- normalized lowercase
    from_name         TEXT,
    from_domain       TEXT,
    to_addr           TEXT,                            -- primary recipient, normalized
    to_all            TEXT,                            -- all To/Cc, comma-joined normalized
    subject           TEXT,
    body_text         TEXT,
    has_attachments   INTEGER NOT NULL DEFAULT 0,
    attachment_names  TEXT,                            -- JSON array of filenames
    size_bytes        INTEGER,
    list_unsubscribe  INTEGER NOT NULL DEFAULT 0,      -- List-Unsubscribe header present
    -- rules stage (sole writer: rules/engine.py)
    ruled_at          TEXT,                            -- set when the rules engine evaluated this row
    rule_name         TEXT,                            -- winning rule from rules.toml (NULL = no match)
    rule_action       TEXT,                            -- keep | archive | trash
    rule_category     TEXT,
    rule_protected    INTEGER NOT NULL DEFAULT 0,      -- a protect=true rule won
    rule_ephemeral    INTEGER NOT NULL DEFAULT 0,      -- winning rule declared the message ephemeral
    -- llm stage (sole writer: llm/classifier.py)
    llm_action        TEXT,                            -- keep | archive | trash | review
    llm_category      TEXT,
    llm_confidence    REAL,
    llm_reason        TEXT,
    llm_ephemeral     INTEGER NOT NULL DEFAULT 0,
    -- final decision (rules engine or classifier, whichever finalizes)
    action            TEXT,                            -- keep | archive | trash | review; NULL = awaiting LLM
    decision_source   TEXT,                            -- 'rule' | 'llm' | 'rule+llm'
    -- review lifecycle (writers: policy.py, review UI, gmail/runner.py)
    review_status     TEXT NOT NULL DEFAULT 'pending', -- pending|approved|auto_approved|rejected|applied|skipped
    review_note       TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(gmail_msgid),
    UNIQUE(rfc_message_id),
    UNIQUE(dedup_key)
);

CREATE INDEX IF NOT EXISTS idx_messages_from_domain ON messages(from_domain);
CREATE INDEX IF NOT EXISTS idx_messages_from_addr   ON messages(from_addr);
CREATE INDEX IF NOT EXISTS idx_messages_date_epoch  ON messages(date_epoch);
CREATE INDEX IF NOT EXISTS idx_messages_action      ON messages(action);
CREATE INDEX IF NOT EXISTS idx_messages_review      ON messages(review_status);
CREATE INDEX IF NOT EXISTS idx_messages_rule        ON messages(rule_name);

-- Full-text search over the message corpus (external-content table).
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, body_text, from_addr, from_name,
    content='messages', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, subject, body_text, from_addr, from_name)
    VALUES (new.id, new.subject, new.body_text, new.from_addr, new.from_name);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, body_text, from_addr, from_name)
    VALUES ('delete', old.id, old.subject, old.body_text, old.from_addr, old.from_name);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF subject, body_text, from_addr, from_name ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, subject, body_text, from_addr, from_name)
    VALUES ('delete', old.id, old.subject, old.body_text, old.from_addr, old.from_name);
    INSERT INTO messages_fts(rowid, subject, body_text, from_addr, from_name)
    VALUES (new.id, new.subject, new.body_text, new.from_addr, new.from_name);
END;

-- Why a message was staged the way it was: one row per rule that matched.
-- Every match is recorded (not just the winner); the policy gates refuse to
-- auto-approve any message with a keep-voting hit, winner or not.
CREATE TABLE IF NOT EXISTS rule_hits (
    id          INTEGER PRIMARY KEY,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    rule_name   TEXT NOT NULL,
    action      TEXT NOT NULL,                          -- keep | archive | trash (the rule's vote)
    won         INTEGER NOT NULL DEFAULT 0,             -- this rule decided the message
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rule_hits_msg  ON rule_hits(message_id);
CREATE INDEX IF NOT EXISTS idx_rule_hits_name ON rule_hits(rule_name);

-- Known contacts: addresses the user has SENT mail to (derived from the MBOX).
CREATE TABLE IF NOT EXISTS contacts (
    address    TEXT PRIMARY KEY,                        -- normalized lowercase
    domain     TEXT,
    sent_count INTEGER NOT NULL DEFAULT 0
);

-- Audit log: every Gmail mutation attempt (including dry runs) is recorded.
CREATE TABLE IF NOT EXISTS actions (
    id               INTEGER PRIMARY KEY,
    message_id       INTEGER NOT NULL REFERENCES messages(id),
    action           TEXT NOT NULL,                     -- trash | archive
    requested_at     TEXT NOT NULL DEFAULT (datetime('now')),
    dry_run          INTEGER NOT NULL,
    reconciled       INTEGER NOT NULL DEFAULT 0,        -- did reconcile confirm the live match
    gmail_api_msgid  TEXT,                              -- live Gmail REST id from reconcile
    match_method     TEXT,                              -- 'rfc822msgid' | 'none'
    match_confirmed  INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL,                     -- attempt | success | skipped | error
    http_status      INTEGER,
    error            TEXT,
    completed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_actions_msg    ON actions(message_id);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
