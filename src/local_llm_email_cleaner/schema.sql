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
-- constraints, so messages missing an identifier never collide.
CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY,
    gmail_msgid       TEXT,                            -- X-GM-MSGID (often absent in Takeout)
    thread_id         TEXT,                            -- X-GM-THRID
    rfc_message_id    TEXT,                            -- RFC 822 Message-ID
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
    -- classification / decision columns
    ai_category       TEXT,
    ai_confidence     REAL,
    ai_reason         TEXT,
    classified_by     TEXT,                            -- 'rules' | 'llm' | 'rules+llm'
    staged_label      TEXT,                            -- KEEP/DELETE_CANDIDATE/ARCHIVE_CANDIDATE/UNSUBSCRIBE_CANDIDATE/NEEDS_REVIEW
    proposed_action   TEXT,                            -- keep | archive | trash | review
    review_status     TEXT NOT NULL DEFAULT 'pending', -- pending|approved|auto_approved|rejected|applied|skipped
    review_note       TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(gmail_msgid),
    UNIQUE(rfc_message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_from_domain ON messages(from_domain);
CREATE INDEX IF NOT EXISTS idx_messages_from_addr   ON messages(from_addr);
CREATE INDEX IF NOT EXISTS idx_messages_date_epoch  ON messages(date_epoch);
CREATE INDEX IF NOT EXISTS idx_messages_proposed    ON messages(proposed_action);
CREATE INDEX IF NOT EXISTS idx_messages_review      ON messages(review_status);
CREATE INDEX IF NOT EXISTS idx_messages_staged      ON messages(staged_label);

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

-- Why a message was staged the way it was: one row per rule that fired.
CREATE TABLE IF NOT EXISTS rule_hits (
    id          INTEGER PRIMARY KEY,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    rule_name   TEXT NOT NULL,
    rule_kind   TEXT NOT NULL,                          -- 'protection' | 'candidate'
    outcome     TEXT NOT NULL,                          -- staged label the rule voted for
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
