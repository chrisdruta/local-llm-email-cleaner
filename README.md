# Local LLM Email Cleaner

A local-LLM-powered Gmail cleanup tool. It stream-parses a Google Takeout MBOX
export into SQLite, classifies messages with deterministic rules plus a local
Ollama model (via LangChain), lets you review every proposal in a Streamlit UI,
and applies only approved actions back to Gmail through the Gmail API.

**Safety model** (background notes in `.claude/design/mbox-ai-integration.md`):

- The agent proposes; a deterministic, audit-logged runner executes only
  approved actions.
- Messages are **trashed, never permanently deleted** — the OAuth scope is
  `gmail.modify`, which cannot call `users.messages.delete` at all.
- The stale MBOX is never trusted: every action is reconciled against live
  Gmail (Message-ID search + metadata confirmation) first; no confident match
  → skip.
- `apply` is a **dry run by default**; mutating Gmail requires `--execute`
  plus an interactive confirmation.

## Pipeline

```
MBOX file → ingest (stream parser) → SQLite (FTS5) → rules engine →
LLM classifier (LangChain + Ollama) → policy gate → review UI / CSV →
Gmail API action runner → trash / archive
```

Every stage reads/writes the SQLite database, so each one is independently
re-runnable and resumable (interrupt `classify` freely; it picks up where it
left off).

## Setup

```bash
uv sync
uv run email-cleaner init --email you@gmail.com   # writes config.toml, creates data/ + the DB
```

Put your Takeout MBOX at `data/takeout.mbox` (or pass a path to `ingest`).

### Ollama (Windows host + devcontainer)

Ollama runs on the native Windows host; the devcontainer reaches it at
`http://host.docker.internal:11434` (the default in `config.toml`). Make sure
the server is running, then check the exact model tag with `ollama list` on
the host and set `[ollama].model` accordingly (default: `gemma3n:e4b`-class).
`classify` fails fast with the server's available tags if the model is missing.
Override per-machine without editing config: `EMAIL_CLEANER_OLLAMA_URL` /
`EMAIL_CLEANER_OLLAMA_MODEL` / `EMAIL_CLEANER_OLLAMA_CONCURRENCY`.

#### Parallel classification

`classify` keeps `[ollama].concurrency` requests in flight at once (default 4;
also `--concurrency N`). That only speeds things up if the Ollama server has
at least that many parallel slots — by default it serves ~4 requests per
loaded model and queues the rest. To raise it, run the helper script from the
repo in a PowerShell window **on the Windows host** (it restarts Ollama with
the settings below, for that process only):

```powershell
.\scripts\start-ollama.ps1                # OLLAMA_NUM_PARALLEL=10
.\scripts\start-ollama.ps1 -Parallel 16   # match [ollama].concurrency
```

Equivalently, set these as system environment variables and restart Ollama:

```
OLLAMA_NUM_PARALLEL=10      # match [ollama].concurrency
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0   # halves KV-cache VRAM
```

Ollama allocates `num_ctx × num_parallel` of KV cache up front, so watch
`ollama ps` after the first batch: if the model shows partial CPU offload,
lower the slot count — spilling weights to CPU costs far more than the
parallelism gains.

### Gmail API credentials (one-time)

1. Create a project at <https://console.cloud.google.com>.
2. **APIs & Services → Library** → enable **Gmail API**.
3. **APIs & Services → OAuth consent screen** → External, add yourself as a
   test user.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** →
   application type **Desktop app**.
5. Download the JSON to `secrets/credentials.json` (gitignored).
6. `uv run email-cleaner auth` — it prints a URL; open it in your host
   browser (VS Code forwards the port back into the container). The token is
   cached at `secrets/token.json`.

## Usage

```bash
uv run email-cleaner ingest               # stream-parse the MBOX into SQLite (idempotent)
uv run email-cleaner rules                # deterministic rules: protect, stage candidates
uv run email-cleaner classify             # local LLM on ambiguous + delete candidates
uv run email-cleaner policy               # auto-trash gate (re-runnable after tuning)
uv run email-cleaner review               # Streamlit UI on :8501 — approve/reject
uv run email-cleaner export actions.csv   # the approved action table
uv run email-cleaner apply                # DRY RUN: reconcile against live Gmail, log
uv run email-cleaner apply --execute      # actually trash/archive approved messages
uv run email-cleaner status               # pipeline progress counts
```

`--limit N` works on `ingest`, `classify`, and `apply` for small trial runs.

Archiving removes the message from the Inbox but keeps it in All Mail, tagged
with the `EmailCleaner/Archived` label (configurable via `[gmail]
archive_label`; set it to `""` to archive without labeling) so cleaner-archived
mail stays easy to find — and bulk-undo — in Gmail.

### How classification is staged

1. **Protection rules** (run first): known contacts (people you've sent mail
   to), financial/legal/medical keywords, security alerts → `KEEP`, excluded
   from the LLM and the gates. Exception: Gmail's own `Spam` label overrides
   the *keyword* protections (scam subjects imitate exactly those topics) but
   never known contacts — and the keyword hit is still recorded, so such
   messages can never be auto-approved, only approved by you in review.
2. **Candidate rules**: promotional/social labels, Gmail's own `Spam` label,
   noreply senders, shipping/calendar notifications, receipts,
   `List-Unsubscribe` newsletters → staged as `DELETE_CANDIDATE` /
   `ARCHIVE_CANDIDATE` / `UNSUBSCRIBE_CANDIDATE`.
3. **LLM** classifies what rules left ambiguous, and gives an independent
   second opinion (confidence score) on rule-staged delete candidates. If the
   LLM disagrees with a delete candidate, the message is demoted to review.
4. **Policy gates** auto-approve:
   - **trash** only when *all* hold: LLM confidence ≥ 0.90, no attachments,
     not a known contact, not protected, older than 12 months, and at least
     one deterministic rule matched;
   - **archive** (laxer — it's reversible and labeled in Gmail) when:
     rule-matched, not a known contact, not protected, and — if the LLM saw
     it — confidence ≥ 0.80 (`auto_archive_min_confidence`; set it above 1 to
     disable auto-archive).

   Everything else waits for you in the review UI.

### Audit trail

- `rule_hits` records why each message was staged.
- `actions` records every Gmail mutation attempt — including dry runs and
  skips — with the reconciliation evidence.

## Development

```bash
uv run pytest          # full test suite (no Ollama or Gmail needed — mocked)
uvx ruff check         # lint
uvx ruff format        # format
```

Tests build a synthetic Takeout-style MBOX on the fly; nothing touches your
real mail.
