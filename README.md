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
uv run email-cleaner init --email you@gmail.com   # writes config.toml + rules.toml, creates data/ + secrets/
```

`init` writes `config.toml` and `rules.toml` and makes the `data/` and
`secrets/` directories — run it once. (It also creates the empty SQLite
schema, but you don't need it for that: `ingest` creates the DB the same way,
so to start over you can just delete `data/email.db` and re-run from
`ingest`.)

`rules.toml` is the user-tunable heart of the pipeline: every deterministic
staging rule lives there (match criteria + action + flags), with comments
explaining the format. Edit it freely; `uv run email-cleaner rules --check`
validates it and reports every problem at once.

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
uv run email-cleaner rules                # 2. deterministic rules.toml staging (incl. Google Voice)
uv run email-cleaner voice-export         # 2.5 OPTIONAL: back up Google Voice to disk before it's trashed
uv run email-cleaner classify             # 3. local LLM: no-rule-match messages + rule verdicts to confirm
uv run email-cleaner policy               # 4. auto-approval gates (re-runnable after tuning)
uv run email-cleaner review               # 5. Streamlit UI on :8501 — review, tune rules/policy, apply
uv run email-cleaner apply                # 6. DRY RUN: reconcile against live Gmail, log only
uv run email-cleaner apply --execute      # 7. actually trash/archive approved messages
```

The review UI (step 5) covers steps 4–7 too: its Rules page re-runs the rules
after you edit `rules.toml`, the Policy page tunes and runs the gates with a
live preview, and the Apply page dry-runs and (after typing `APPLY`) executes
against live Gmail — the CLI commands remain for scripted use.

`uv run email-cleaner status` prints pipeline progress counts at any point, and
`uv run email-cleaner export actions.csv` dumps the approved-action table for
inspection (optional — not on the critical path).

**Why this order** — each stage only consumes what the previous one produced:

- `ingest` must populate the message DB before anything can classify it (and it
  creates `data/email.db` if it's missing).
- `rules` runs *before* the LLM on purpose: protect rules pull known-contact
  mail out so the LLM never sees it, and the cleanup rules stage the easy
  promos (most with `confirm_with_llm`, so the LLM double-checks them).
- `rules` also stages any Google Voice SMS / call-log / voicemail records for
  trash (the `voice` rule decides alone — the LLM never judges them).
  `voice-export` is a *separate, optional* on-disk backup of those messages —
  run it any time after `ingest` but **before `apply`** if you want to keep a
  copy, since `apply` is what actually trashes them.
- `classify` touches exactly the rows without a final action: messages no rule
  matched (the LLM suggests the action) plus rule verdicts awaiting
  confirmation. The LLM never sees the rule's verdict — agreement confirms it,
  disagreement routes the message to human review.
- `policy` is the *only* place auto-approval happens, so it runs after
  `classify` has produced confidence scores.
- `review` → `apply` is the human gate, then the deterministic action runner.

**Re-running / tuning loop** — every stage reads/writes the same SQLite DB, so
each is independently re-runnable and resumable: interrupt `classify` freely
(it picks up where it left off), and re-run `policy` after tuning thresholds.
After editing `rules.toml`, run `rules --reset` (or the UI's re-run button):
pending rows are re-staged against the new rules while **stored LLM verdicts
are kept and re-finalized** — tuning never re-pays LLM time; only rows newly
needing an opinion wait for the next `classify` (`--reset --full` wipes the
verdicts too). For a genuinely clean slate, delete `data/email.db` and re-run
from `ingest` (which recreates it) — you don't need to re-run `init`.

`--limit N` works on `ingest`, `classify`, and `apply` for small trial runs.

### Google Voice export

If your mailbox includes Google Voice SMS / call-log emails (labelled `SMS` and
`Call log`, with the other party as a synthetic `<number>@unknown.email`
sender), the `rules` stage stages them for trash (the `voice` rule), and
`voice-export` backs them up to disk in a clean, re-importable form first:

```bash
uv run email-cleaner voice-export                 # to [voice].out_dir (default data/voice-export)
uv run email-cleaner voice-export --out /mnt/backup/voice
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

`voice-export` only writes the disk backup — it never touches the database.
Staging Voice messages for trash is the `rules` stage's job: the `voice` rule
(priority 1000 in `rules.toml`) stages them for trash without LLM involvement.
They can never be auto-approved — the auto-trash gate requires an LLM
confidence they don't have — so they wait `pending` for your explicit approval
in review before `apply` trashes anything. The output files are rewritten in
full on every run, so it's safe to re-run. **Run `voice-export` before
`apply`** if you want the backup.

Archiving removes the message from the Inbox but keeps it in All Mail, tagged
with the `EmailCleaner/Archived` label (configurable via `[gmail]
archive_label`; set it to `""` to archive without labeling) so cleaner-archived
mail stays easy to find — and bulk-undo — in Gmail.

### How classification is staged

1. **Rules** (`rules.toml`, run first): every enabled rule is tested against
   every message; all matches are recorded and the highest-priority match
   wins. The winner's `action` (keep / archive / trash) becomes the staged
   verdict. Three flags control what happens next:
   - `protect = true` (e.g. `known_contact`) — absolute keep: never sent to
     the LLM, and *any* keep-voting match (winner or not) blocks
     auto-approval. The default file puts Gmail's `Spam` label above the
     financial/security keyword keeps (scam bait imitates exactly those
     topics), but the recorded keep hit still forces human review.
   - `confirm_with_llm = true` (most cleanup rules) — the verdict is
     tentative until the LLM independently agrees.
   - neither — the rule decides alone (e.g. `voice`).
2. **LLM** classifies every ruled message without a final action: rows no
   rule matched (its suggestion stands) and rule verdicts awaiting
   confirmation. It never sees the rule's verdict; agreement confirms the
   action, any disagreement routes the message to human review.
3. **Policy gates** auto-approve (tunable in `[policy]` config or live on the
   UI's Policy page, which previews exactly what would be approved):
   - **trash** only when *all* hold: staged by a trash rule AND confirmed by
     the LLM at confidence ≥ 0.90 — or, for messages **no rule matched**, the
     LLM alone at the higher `auto_llm_only_min_confidence` bar (default
     0.95; set above 1 to disable) — plus no attachments, not a known
     contact, no keep-voting rule hit, older than 12 months (digests flagged
     ephemeral by both the rule and the LLM only need 7 days);
   - **archive** (laxer — it's reversible and labeled in Gmail) when: staged
     by an archive rule, not a known contact, no keep-voting hit, and — if
     the LLM saw it — confidence ≥ 0.80 (`auto_archive_min_confidence`; set
     it above 1 to disable auto-archive). No-rule-matched archives use the
     same higher LLM-only bar as trash.

   Everything else waits for you in the review UI. The Review page's presets
   surface exactly what needs human eyes: rule/LLM disagreements (prime
   rule-tuning input), low-confidence verdicts, and messages no rule matched.

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
