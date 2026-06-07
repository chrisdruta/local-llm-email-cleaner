# Local LLM email box cleaning considerations

> Status: design notes / suggestions from the initial brainstorm. Not
> binding — the implementation and CLAUDE.md reflect current decisions, and
> they may deviate from these notes where a better approach exists.
The best architecture is **not** “let the agent directly chew through the 1 GB MBOX and delete things.” Use a **two-stage, auditable pipeline**:

1. **Parse + index the MBOX locally**
2. **Have the AI agent classify / propose actions**
3. **Review the proposed deletion set**
4. **Apply changes to Gmail through the Gmail API using message IDs or Gmail search queries**
5. **Trash first, permanently delete later**

For Gmail specifically, use `messages.trash`, not permanent deletion. Google’s Gmail API docs say `users.messages.trash` moves a message to trash, while `users.messages.delete` “immediately and permanently deletes” a message and cannot be undone; Google explicitly says to prefer trash instead. ([Google for Developers][1]) ([Google for Developers][2])

## Recommended setup

### Local side

Use Python with:

```text
mailbox / email.parser
sqlite
tantivy, sqlite FTS5, or ripgrep-style text index
sentence-transformers or local embeddings
Ollama / llama.cpp / local agent framework
```

A 1 GB MBOX is very manageable locally, but do **streaming parse**, not “load whole file into memory.” Python’s standard `mailbox` module can parse MBOX files, though Google Takeout MBOX files can have edge cases; there are also purpose-built parsers like `mbox-parser` that advertise handling “real-life mbox files,” including Google Takeout exports. ([Covrebo][3]) ([GitHub][4])

Store each message in SQLite with fields like:

```sql
gmail_msgid
rfc_message_id
thread_id
date
from_addr
to_addr
subject
labels
snippet
body_text
has_attachments
attachment_names
size_bytes
ai_category
ai_confidence
proposed_action
review_status
```

The important part is preserving identifiers. In Google Takeout MBOX, look for headers such as:

```text
X-GM-MSGID
X-GM-THRID
X-Gmail-Labels
Message-ID
```

`X-GM-MSGID` is especially useful when present because it gives you a Gmail-side identifier to reconcile the local export with Gmail. If it is missing or hard to use, fall back to matching by `Message-ID`, date, sender, and subject, then confirm via Gmail API search.

## Agent workflow

Do **not** ask the model to decide deletion in one pass. Use a staged classifier:

```text
KEEP
DELETE_CANDIDATE
ARCHIVE_CANDIDATE
UNSUBSCRIBE_CANDIDATE
NEEDS_REVIEW
```

Start with deterministic rules first:

```text
from:(noreply OR no-reply)
older than X years
large attachments
promotional labels
receipts
shipping notifications
calendar notifications
social notifications
security alerts
financial/legal/medical keywords
personal contacts
```

Then let the local model classify ambiguous messages.

A good prompt pattern:

```text
You are classifying email for cleanup. 
Return JSON only.

Possible actions:
- keep
- archive
- trash
- review

Never trash:
- legal, tax, medical, financial, account security
- personal correspondence
- receipts for expensive items
- active subscriptions or login/security alerts
- anything with uncertainty

Email:
From: ...
Subject: ...
Date: ...
Labels: ...
Body excerpt: ...

Return:
{
  "action": "...",
  "category": "...",
  "confidence": 0.0-1.0,
  "reason": "short reason"
}
```

Then enforce a policy like:

```text
Only auto-trash when:
action == "trash"
confidence >= 0.90
no attachments
not from a known contact
not financial/legal/security
older than N months
matches at least one deterministic cleanup rule
```

Everything else goes to review.

Amendment (2026-06-07): messages carrying Gmail's own `Spam` label are staged
`DELETE_CANDIDATE` even when the financial/legal/security keyword rules match
the subject (scam bait imitates exactly those topics). The suppressed keyword
hits are still recorded as protection hits, so such messages can never be
auto-approved — they always require human review. Known-contact protection
remains absolute.

## Best deletion interface with Gmail

The safest path is:

### Option A — use Gmail search queries for bulk classes

For obvious categories, generate Gmail queries and apply actions through Gmail API:

```text
from:(newsletter@example.com) older_than:2y
category:promotions older_than:3y
subject:(sale OR discount OR coupon) older_than:1y
```

This is safer than matching individual MBOX messages when the MBOX is stale.

### Option B — use message-level reconciliation

For messages classified from the MBOX, map local records back to Gmail API message IDs.

Flow:

```text
MBOX message
→ extract X-GM-MSGID / Message-ID
→ Gmail API search or lookup
→ confirm metadata match
→ call users.messages.trash
```

Avoid permanent delete. Trash first, then review Gmail Trash manually or run a second deletion phase weeks later.

## Local AI agent shape

A practical architecture:

```text
MBOX file
   ↓
stream parser
   ↓
SQLite message DB
   ↓
text normalization + attachment metadata
   ↓
rules engine
   ↓
local LLM classifier
   ↓
review UI / CSV
   ↓
Gmail API action runner
   ↓
trash/archive/label
```

For the review UI, simplest is a local Streamlit app or Datasette over SQLite. Include filters like:

```text
show all proposed trash
group by sender
group by domain
show largest senders
show oldest promotional mail
show uncertain classifications
```

Then export an action table:

```csv
gmail_message_id,action,reason,confidence
18c...,trash,"old promo newsletter",0.96
18d...,archive,"old notification",0.91
```

## What I would avoid

Avoid letting an agent directly delete via browser automation. Gmail UI automation is brittle and hard to audit.

Avoid using only embeddings. Embeddings are good for clustering newsletters and finding similar junk, but deletion needs explicit rules and exact metadata.

Avoid permanent deletion in the first pass. Gmail API permanent deletion is irreversible. ([Google for Developers][2])

Avoid trusting the MBOX as live state. The Takeout file may be stale; always reconcile with current Gmail before acting.

## My preferred stack

For a robust but not overbuilt version:

```text
Python
mailbox or mbox-parser
SQLite + FTS5
Ollama with qwen2.5, llama3.1, or mistral-class local model
Streamlit review UI
Google Gmail API
```

Use the AI for **classification and clustering**, not final authority.

The key design rule: **the agent proposes; a deterministic action runner executes only approved, logged actions.**

[1]: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/trash?utm_source=chatgpt.com "Method: users.messages.trash | Gmail"
[2]: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/delete?utm_source=chatgpt.com "Method: users.messages.delete | Gmail"
[3]: https://covrebo.com/parsing-mbox-files-with-the-mailbox-library.html?utm_source=chatgpt.com "Parsing mbox Files with the Mailbox Library - COvrebo"
[4]: https://github.com/elte-dh/mbox-parser?utm_source=chatgpt.com "ELTE-DH/mbox-parser"
