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

## Setup

One-time, per machine:

```bash
uv sync
uv run email-cleaner init --email you@gmail.com   # writes config.toml, creates data/ + secrets/
```

`init` writes `config.toml` and makes the `data/` and `secrets/` directories —
run it once. (It also creates the empty SQLite schema, but you don't need it
for that: `ingest` creates the DB the same way, so to start over you can just
delete `data/email.db` and re-run from `ingest`.)

Then put your Takeout MBOX at `data/takeout.mbox` (or pass a path to `ingest`),
and set up Ollama and Gmail credentials below.

### Ollama (Windows host + devcontainer)

Ollama runs on the native Windows host; the devcontainer reaches it at
`http://host.docker.internal:11434` (the default in `config.toml`). Make sure
the server is running, then check the exact model tag with `ollama list` on
the host and set `[ollama].model` accordingly (default: `gemma4:e4b-it-q8_0`-class).
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
.\scripts\start-ollama.ps1                # OLLAMA_NUM_PARALLEL=4 (default)
.\scripts\start-ollama.ps1 -Parallel 8    # raise it; match [ollama].concurrency
```

Equivalently, set these as system environment variables and restart Ollama:

```
OLLAMA_NUM_PARALLEL=4       # match [ollama].concurrency
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0   # halves KV-cache VRAM
```

Ollama allocates `num_ctx × num_parallel` of KV cache up front, so watch
`ollama ps` after the first batch: if the model shows partial CPU offload,
lower the slot count — spilling weights to CPU costs far more than the
parallelism gains. A q8_0 *weight* quant (e.g. `gemma…:e4b-it-q8_0`) loads at
roughly double the Q4 size, leaving less room for slots — raise `-Parallel`
(and `[ollama].concurrency` to match) only as far as `ollama ps` stays 100% GPU.

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

With [Setup](#setup) done, put your Takeout MBOX at `data/takeout.mbox` (or
pass a path to `ingest`) and run the pipeline in order:

```bash
uv run email-cleaner ingest               # 1. stream-parse the MBOX into SQLite (creates the DB; idempotent)
uv run email-cleaner rules                # 2. deterministic rules: protect, stage candidates
uv run email-cleaner voice-export         # 2.5 OPTIONAL: back up Google Voice, stage it for trash
uv run email-cleaner classify             # 3. local LLM on ambiguous + delete candidates
uv run email-cleaner policy               # 4. auto-approval gate (re-runnable after tuning)
uv run email-cleaner review               # 5. Streamlit UI on :8501 — approve/reject
uv run email-cleaner apply                # 6. DRY RUN: reconcile against live Gmail, log only
uv run email-cleaner apply --execute      # 7. actually trash/archive approved messages
```

`uv run email-cleaner status` prints pipeline progress counts at any point, and
`uv run email-cleaner export actions.csv` dumps the approved-action table for
inspection (optional — not on the critical path).

**Why this order** — each stage only consumes what the previous one produced:

- `ingest` must populate the message DB before anything can classify it (and it
  creates `data/email.db` if it's missing).
- `rules` runs *before* the LLM on purpose: protection rules pull
  known-contact / financial / security mail out so the LLM never sees it, and
  candidate rules stage the easy promos.
- `voice-export`, if you use it, goes after `rules` but before `classify` — it
  stages Voice messages and tags them so the classifier skips them.
- `classify` then only touches what rules left ambiguous (plus a second opinion
  on delete candidates).
- `policy` is the *only* place auto-approval happens, so it runs after
  `classify` has produced confidence scores.
- `review` → `apply` is the human gate, then the deterministic action runner.

**Re-running** — every stage reads/writes the same SQLite DB, so each is
independently re-runnable and resumable: interrupt `classify` freely (it picks
up where it left off), and re-run `policy` after tuning thresholds. For a
genuinely clean slate, delete `data/email.db` and re-run from `ingest` (which
recreates it) — you don't need to re-run `init`.

`--limit N` works on `ingest`, `classify`, and `apply` for small trial runs.

### Google Voice export

If your mailbox includes Google Voice SMS / call-log emails (labelled `SMS` and
`Call log`, with the other party as a synthetic `<number>@unknown.email`
sender), `voice-export` backs them up to disk in a clean, re-importable form and
then stages them for trash so they leave Gmail:

```bash
uv run email-cleaner voice-export                 # to [voice].out_dir (default data/voice-export)
uv run email-cleaner voice-export --out /mnt/backup/voice
uv run email-cleaner voice-export --no-trash      # back up only, leave Gmail untouched
uv run email-cleaner voice-export --mbox path.mbox  # explicit source for attachment recovery
uv run email-cleaner voice-export --no-attachments  # text only, skip image recovery
```

It writes `sms.jsonl` + `calls.jsonl` (one normalized record per line),
per-contact transcripts under `sms/<contact>.md`, and a `calls.csv`. SMS
direction is recovered from the sender (a real-domain From means *you* sent it);
call direction and duration come from the body.

**Attachments (MMS images):** ingest keeps only attachment *filenames*, not the
bytes, so voice-export re-reads the source MBOX (located automatically from the
ingest record, or via `--mbox`) and recovers the images to
`attachments/<contact>/`, referenced from each record's `attachments` array and
the transcripts. If the MBOX isn't reachable, text still exports and the
affected records are flagged `"exported": false`.

Unless `--no-trash`, exported messages are staged `DELETE_CANDIDATE` (still
`pending` — they go through the normal review/`apply` flow before anything is
trashed) and tagged so the LLM classifier skips them. The output files are
rewritten in full on every run, so it's safe to re-run.

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
   second opinion (confidence score) on rule-staged **delete and archive**
   candidates. If the LLM disagrees with a delete candidate, it's demoted to
   review. For an archive candidate the LLM may agree (stays archive, with its
   confidence), **escalate to trash** (handed to the stricter auto-trash gate),
   or disagree (demoted to review).
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
  skips — with the reconciliation evidence. Live mutations are
  write-intent-then-mark-success: an `attempt` row is committed *before* the
  Gmail call and finalized to `success`/`error` after, so a crash mid-apply
  leaves a visible `attempt` row (NULL `completed_at`) instead of a silent
  gap. Dry runs log everything but never change review state.
- Trash is applied per message (`users.messages.trash`); archives are
  batched through `users.messages.batchModify` (up to 1000 ids per call). A
  failed batch marks its chunk `error` and leaves the rows approved, so
  re-running `apply --execute` retries them.

## Development

```bash
uv run pytest          # full test suite (no Ollama or Gmail needed — mocked)
uvx ruff check         # lint
uvx ruff format        # format
```

Tests build a synthetic Takeout-style MBOX on the fly; nothing touches your
real mail.
