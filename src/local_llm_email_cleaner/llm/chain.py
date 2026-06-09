"""LangChain classifier chain: prompt | ChatOllama with structured output."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from langchain_core.runnables import Runnable
from langchain_ollama import ChatOllama

from ..config import Config
from .prompts import CLASSIFY_PROMPT
from .schema import EmailClassification

logger = logging.getLogger(__name__)


class OllamaUnavailableError(RuntimeError):
    """The Ollama server can't be reached (or lacks the configured model)."""


def list_models(base_url: str, timeout: float = 10.0) -> list[str]:
    """Model tags available on the Ollama server (connectivity probe)."""
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise OllamaUnavailableError(
            f"Ollama is not reachable at {base_url} ({exc}). "
            "Is the server running on the host? From the devcontainer it is usually "
            "http://host.docker.internal:11434 — override with EMAIL_CLEANER_OLLAMA_URL "
            "or [ollama].url in config.toml."
        ) from exc
    return [m.get("name", "") for m in data.get("models", [])]


def check_model_available(cfg: Config) -> None:
    """Fail fast on unreachable server; warn (with the tag list) on missing model."""
    tags = list_models(cfg.ollama_url)
    if cfg.ollama_model not in tags and not any(
        t.split(":")[0] == cfg.ollama_model for t in tags
    ):
        logger.warning(
            "Model %r not in Ollama's tag list %s — `ollama pull %s` on the host, "
            "or set [ollama].model to one of the listed tags.",
            cfg.ollama_model,
            tags,
            cfg.ollama_model,
        )


def build_classifier_chain(cfg: Config) -> Runnable:
    """prompt | ChatOllama -> EmailClassification (schema-constrained decoding)."""
    llm = ChatOllama(
        base_url=cfg.ollama_url,
        model=cfg.ollama_model,
        temperature=0,  # already set — right call for classification
        num_ctx=4096,  # context window; prompts run ~1.7k tok (body capped at
        # max_body_chars + 256 output), so 4096 keeps a 2.4x margin while
        # halving KV cache vs 8192 — frees VRAM for more parallel slots
        num_predict=256,  # output cap
        keep_alive="10m",  # already set — avoids a ~10s model reload per call
        reasoning=False,  # thinking models would burn num_predict on thinking
        # tokens and return empty content (parse failure)
        # Per-request timeout on the underlying ollama HTTP client, so a stalled
        # generation fails (and the classifier retries) instead of hanging a
        # worker thread forever.
        client_kwargs={"timeout": cfg.request_timeout_s},
    )
    structured = llm.with_structured_output(EmailClassification, method="json_schema")
    return CLASSIFY_PROMPT | structured
