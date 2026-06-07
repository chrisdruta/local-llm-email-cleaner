"""MIME body extraction: best-effort plain text, snippet, attachment metadata."""

from __future__ import annotations

import logging
from email.message import Message
from html.parser import HTMLParser

logger = logging.getLogger(__name__)

SNIPPET_CHARS = 300
MAX_BODY_CHARS = 50_000  # cap stored body text; classification uses far less


class _TextExtractor(HTMLParser):
    """Minimal HTML→text: drops tags, scripts, and styles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        return " ".join(self._chunks)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # tolerate malformed markup; keep whatever was extracted
        pass
    return parser.text()


def _decode_part(part: Message) -> str | None:
    payload = part.get_payload(decode=True)
    if payload is None:
        return None
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:  # unknown charset label
        return payload.decode("utf-8", errors="replace")


def _is_attachment(part: Message) -> bool:
    if part.get_content_disposition() == "attachment":
        return True
    # Inline parts with filenames (images, PDFs) still count as attachments.
    return part.get_filename() is not None and part.get_content_maintype() != "text"


def extract_body(msg: Message) -> tuple[str, bool, list[str]]:
    """Walk the MIME tree; return (normalized text, has_attachments, attachment names)."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[str] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        if _is_attachment(part):
            name = part.get_filename()
            attachments.append(
                name if name else f"unnamed.{part.get_content_subtype()}"
            )
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            decoded = _decode_part(part)
            if decoded:
                plain_parts.append(decoded)
        elif ctype == "text/html":
            decoded = _decode_part(part)
            if decoded:
                html_parts.append(decoded)

    if plain_parts:
        text = "\n".join(plain_parts)
    elif html_parts:
        text = html_to_text("\n".join(html_parts))
    else:
        text = ""

    text = normalize_text(text)[:MAX_BODY_CHARS]
    return text, bool(attachments), attachments


def normalize_text(text: str) -> str:
    """Collapse runs of whitespace while keeping line structure readable."""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1]:  # collapse blank runs to a single separator
            out.append("")
    return "\n".join(out).strip()


def make_snippet(body_text: str) -> str:
    return " ".join(body_text.split())[:SNIPPET_CHARS]
