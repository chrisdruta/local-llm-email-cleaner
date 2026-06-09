# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A local-LLM-powered Gmail cleanup tool. It parses a Google Takeout MBOX export, classifies messages locally, and applies approved actions back to Gmail via the Gmail API. The design document `.claude/design/mbox-ai-integration.md` holds the original design notes — useful background and suggestions, but not binding; the implementation and this file are what count, and features may deviate from the notes when there's a better approach. The full pipeline is implemented; see README.md for the user-facing walkthrough.

**Never run or test against the user's live data.** The real Takeout MBOX, the working database (`data/email.db`), `secrets/`, and the live Gmail account are off-limits for testing, debugging, or verification — verify changes only with the synthetic fixtures in `tests/conftest.py` (or a fresh throwaway DB built from them). Running pipeline commands against real data is a user-only action.

## Commands

Packaged uv project (Python 3.12, `src/` layout, `uv_build` backend). The console script `email-cleaner` maps to `local_llm_email_cleaner:main`, a Click group with one subcommand per pipeline stage (`init`, `ingest`, `rules`, `classify`, `policy`, `review`, `export`, `auth`, `apply`, `status`):

```bash
uv sync                      # install/sync dependencies
uv run email-cleaner --help  # run the app
uv run pytest                # test suite (Ollama and Gmail are mocked)
uv add <package>             # add a dependency
```

There is no committed lint config; use `uvx ruff check` / `uvx ruff format` (the devcontainer ships the Ruff VS Code extension).

## Code layout (src/local_llm_email_cleaner/)

`config.py` (TOML + `EMAIL_CLEANER_*` env overrides) · `db.py`/`schema.sql` (SQLite, FTS5, audit tables) · `ingest/` (streaming MBOX → SQLite, known-contact derivation from Sent mail) · `rules/` (protection rules are absolute; candidate rules stage cleanup classes; patterns live in `patterns.py` as data) · `llm/` (LangChain: `ChatOllama` + Pydantic `with_structured_output`; classifier is batched/resumable) · `policy.py` (the only place auto-approval can happen) · `review/` (Streamlit UI; writes `review_status` only) · `gmail/` (OAuth `gmail.modify` scope only, rfc822msgid reconciliation, dry-run-default runner). Tests build a synthetic mbox in `tests/conftest.py`.

## Environment

Development happens inside a VS Code dev container (see `.devcontainer/`): a Python 3.12 Debian (bullseye) image with uv and Claude Code installed. System-level dependencies must be added to `.devcontainer/Dockerfile`, which requires asking the user to rebuild the container — they can't be installed persistently from inside a session.

## Architecture

Two-stage, auditable pipeline — **the agent proposes; a deterministic action runner executes only approved, logged actions**:

```
MBOX file → stream parser → SQLite message DB → rules engine →
local LLM classifier → review UI/CSV → Gmail API action runner → trash/archive/label
```

Key design rules:

- **Stream-parse the MBOX** (Python `mailbox` or `mbox-parser`); never load the ~1 GB file into memory.
- **Preserve Gmail identifiers** from Takeout headers (`X-GM-MSGID`, `X-GM-THRID`, `X-Gmail-Labels`, `Message-ID`) in SQLite so local records can be reconciled with live Gmail. Fall back to Message-ID + date/sender/subject matching when `X-GM-MSGID` is absent.
- **Deterministic rules run first**; the local LLM (Ollama with a qwen2.5/llama3.1/mistral-class model) only classifies ambiguous messages, returning JSON `{action, category, confidence, reason}`.
- **Staged classification labels**: KEEP / DELETE_CANDIDATE / ARCHIVE_CANDIDATE / UNSUBSCRIBE_CANDIDATE / NEEDS_REVIEW. Auto-trash only with high confidence (≥0.90), no attachments, not from known contacts, not financial/legal/security, and matching at least one deterministic rule — everything else goes to review.
- **Trash, never permanently delete**: use Gmail API `users.messages.trash`, not `users.messages.delete`. Permanent deletion is a separate, later phase after manual review.
- **Don't trust the MBOX as live state** — always reconcile against current Gmail (API search/lookup, metadata match) before acting.
- No browser automation against the Gmail UI; all Gmail mutations go through the Gmail API.

Stack: Python, stdlib `mailbox`, SQLite + FTS5, LangChain (`langchain-ollama`) against an Ollama server on the native Windows host (`http://host.docker.internal:11434` from the devcontainer), Streamlit (review UI), Gmail API. LangChain is used for the LLM classification layer only — pipeline orchestration stays deterministic.
