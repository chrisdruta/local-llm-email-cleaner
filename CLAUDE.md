# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A local-LLM-powered Gmail cleanup tool. It parses a Google Takeout MBOX export, classifies messages locally, and applies approved actions back to Gmail via the Gmail API. The implementation is greenfield (the entrypoint is a stub); the design document `mbox-ai-integration.md` is the source of truth for the intended architecture and must be followed when building features.

## Commands

Packaged uv project (Python 3.12, `src/` layout, `uv_build` backend). The console script `email-cleaner` maps to `local_llm_email_cleaner:main`:

```bash
uv sync               # install/sync dependencies
uv run email-cleaner  # run the app
uv add <package>      # add a dependency
```

There is no test suite or lint configuration yet. The devcontainer ships the Ruff VS Code extension, so use `uvx ruff check` / `uvx ruff format` for linting and formatting.

## Environment

Development happens inside a VS Code dev container (see `.devcontainer/`): a Python 3.12 Debian (bullseye) image with uv and Claude Code installed. System-level dependencies must be added to `.devcontainer/Dockerfile`, which requires asking the user to rebuild the container — they can't be installed persistently from inside a session.

## Architecture (from mbox-ai-integration.md)

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

Intended stack: Python, `mailbox`/`mbox-parser`, SQLite + FTS5, Ollama, Streamlit (review UI), Gmail API.
