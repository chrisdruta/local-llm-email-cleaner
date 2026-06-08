"""Configuration loading: built-in defaults < config.toml < EMAIL_CLEANER_* env vars."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

DEFAULT_CONFIG_FILENAME = "config.toml"

CONFIG_TEMPLATE = """\
# local-llm-email-cleaner configuration.
# Env vars override file values: EMAIL_CLEANER_DB, EMAIL_CLEANER_OLLAMA_URL,
# EMAIL_CLEANER_OLLAMA_MODEL, EMAIL_CLEANER_OLLAMA_CONCURRENCY,
# EMAIL_CLEANER_CONFIG (alternate path to this file).

[paths]
db = "data/email.db"
mbox = "data/takeout.mbox"
credentials = "secrets/credentials.json"
token = "secrets/token.json"

[ollama]
# From inside the devcontainer, the Windows-host Ollama server is reachable via
# host.docker.internal. Run `ollama list` on the host for the exact model tag.
url = "http://host.docker.internal:11434"
model = "gemma4:e4b-it-q8_0"
max_body_chars = 3000
batch_size = 100
request_timeout_s = 180
# Classification requests in flight at once. Only helps beyond the server's
# slot count if Ollama is started with OLLAMA_NUM_PARALLEL >= this value
# (extra requests just queue server-side, which is harmless).
concurrency = 4

[rules]
# Your own address(es) — used to find Sent mail and derive known contacts.
user_addresses = [{user_addresses}]

[policy]
auto_trash_min_confidence = 0.90
auto_trash_min_age_months = 12
# Archive auto-approval (reversible; archived mail gets [gmail].archive_label).
# Applies to rule-staged archive candidates and, when the LLM weighed in, only
# above this confidence. Set to a value > 1 to disable auto-archive entirely.
auto_archive_min_confidence = 0.80

[gmail]
oauth_port = 8765
requests_per_second = 5
uncertain_confidence_threshold = 0.75
# Gmail label added to messages the runner archives (created on first use),
# so they stay easy to find — and bulk-undo — in Gmail. "" disables labeling.
archive_label = "EmailCleaner/Archived"

[voice]
# `email-cleaner voice-export` writes Google Voice SMS / call-log backups here.
out_dir = "data/voice-export"
# After exporting to disk, stage those messages for trash (still pending — they
# go through normal review/approval first). Set false to back up only.
trash_after_export = true
"""


@dataclass(frozen=True)
class Config:
    # paths
    db_path: Path
    mbox_path: Path
    credentials_path: Path
    token_path: Path
    # ollama
    ollama_url: str
    ollama_model: str
    max_body_chars: int
    llm_batch_size: int
    llm_concurrency: int
    request_timeout_s: float
    # rules
    user_addresses: tuple[str, ...]
    # policy
    auto_trash_min_confidence: float
    auto_trash_min_age_months: int
    auto_archive_min_confidence: float
    # gmail
    oauth_port: int
    requests_per_second: float
    uncertain_confidence_threshold: float
    archive_label: str
    # voice
    voice_out_dir: Path
    voice_trash_after_export: bool


DEFAULTS = Config(
    db_path=Path("data/email.db"),
    mbox_path=Path("data/takeout.mbox"),
    credentials_path=Path("secrets/credentials.json"),
    token_path=Path("secrets/token.json"),
    ollama_url="http://host.docker.internal:11434",
    ollama_model="gemma4:e4b-it-q8_0",
    max_body_chars=3000,
    llm_batch_size=100,
    llm_concurrency=4,
    request_timeout_s=180.0,
    user_addresses=(),
    auto_trash_min_confidence=0.90,
    auto_trash_min_age_months=12,
    auto_archive_min_confidence=0.80,
    oauth_port=8765,
    requests_per_second=5.0,
    uncertain_confidence_threshold=0.75,
    archive_label="EmailCleaner/Archived",
    voice_out_dir=Path("data/voice-export"),
    voice_trash_after_export=True,
)


def _from_toml(cfg: Config, data: dict) -> Config:
    paths = data.get("paths", {})
    ollama = data.get("ollama", {})
    rules = data.get("rules", {})
    policy = data.get("policy", {})
    gmail = data.get("gmail", {})
    voice = data.get("voice", {})
    return replace(
        cfg,
        db_path=Path(paths.get("db", cfg.db_path)),
        mbox_path=Path(paths.get("mbox", cfg.mbox_path)),
        credentials_path=Path(paths.get("credentials", cfg.credentials_path)),
        token_path=Path(paths.get("token", cfg.token_path)),
        ollama_url=ollama.get("url", cfg.ollama_url),
        ollama_model=ollama.get("model", cfg.ollama_model),
        max_body_chars=int(ollama.get("max_body_chars", cfg.max_body_chars)),
        llm_batch_size=int(ollama.get("batch_size", cfg.llm_batch_size)),
        llm_concurrency=int(ollama.get("concurrency", cfg.llm_concurrency)),
        request_timeout_s=float(ollama.get("request_timeout_s", cfg.request_timeout_s)),
        user_addresses=tuple(
            addr.strip().lower()
            for addr in rules.get("user_addresses", cfg.user_addresses)
        ),
        auto_trash_min_confidence=float(
            policy.get("auto_trash_min_confidence", cfg.auto_trash_min_confidence)
        ),
        auto_trash_min_age_months=int(
            policy.get("auto_trash_min_age_months", cfg.auto_trash_min_age_months)
        ),
        auto_archive_min_confidence=float(
            policy.get("auto_archive_min_confidence", cfg.auto_archive_min_confidence)
        ),
        oauth_port=int(gmail.get("oauth_port", cfg.oauth_port)),
        requests_per_second=float(
            gmail.get("requests_per_second", cfg.requests_per_second)
        ),
        uncertain_confidence_threshold=float(
            gmail.get(
                "uncertain_confidence_threshold", cfg.uncertain_confidence_threshold
            )
        ),
        archive_label=str(gmail.get("archive_label", cfg.archive_label)).strip(),
        voice_out_dir=Path(voice.get("out_dir", cfg.voice_out_dir)),
        voice_trash_after_export=bool(
            voice.get("trash_after_export", cfg.voice_trash_after_export)
        ),
    )


def _from_env(cfg: Config) -> Config:
    env = os.environ
    updates: dict = {}
    if "EMAIL_CLEANER_DB" in env:
        updates["db_path"] = Path(env["EMAIL_CLEANER_DB"])
    if "EMAIL_CLEANER_OLLAMA_URL" in env:
        updates["ollama_url"] = env["EMAIL_CLEANER_OLLAMA_URL"]
    if "EMAIL_CLEANER_OLLAMA_MODEL" in env:
        updates["ollama_model"] = env["EMAIL_CLEANER_OLLAMA_MODEL"]
    if "EMAIL_CLEANER_OLLAMA_CONCURRENCY" in env:
        raw = env["EMAIL_CLEANER_OLLAMA_CONCURRENCY"]
        try:
            updates["llm_concurrency"] = int(raw)
        except ValueError:
            raise ValueError(
                f"EMAIL_CLEANER_OLLAMA_CONCURRENCY must be an integer, got {raw!r}"
            ) from None
    return replace(cfg, **updates) if updates else cfg


def config_file_path(explicit: str | Path | None = None) -> Path:
    """Resolve the config file path: explicit arg > EMAIL_CLEANER_CONFIG > ./config.toml."""
    if explicit:
        return Path(explicit)
    if "EMAIL_CLEANER_CONFIG" in os.environ:
        return Path(os.environ["EMAIL_CLEANER_CONFIG"])
    return Path(DEFAULT_CONFIG_FILENAME)


def load_config(explicit_path: str | Path | None = None) -> Config:
    cfg = DEFAULTS
    path = config_file_path(explicit_path)
    if path.is_file():
        with path.open("rb") as fh:
            cfg = _from_toml(cfg, tomllib.load(fh))
    return _from_env(cfg)


def write_default_config(path: Path, user_addresses: tuple[str, ...] = ()) -> None:
    """Write a fresh config file (used by `init` and for config.example.toml)."""
    rendered = CONFIG_TEMPLATE.format(
        user_addresses=", ".join(f'"{addr}"' for addr in user_addresses)
    )
    path.write_text(rendered, encoding="utf-8")
