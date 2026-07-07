"""Shared helpers for public-JSON joblister plugins (no auth required).

Factored out of the original inline ``remoteok.py`` implementation so the five
sibling plugins (remotive, arbeitnow, jobicy, himalayas, themuse) don't each
duplicate HTML-stripping and keyword-matching. Leading underscore keeps the
registry from treating this file as a plugin (registry.py skips ``_*``).
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "application/json",
}
TIMEOUT = 20  # seconds


def strip_html(raw: str) -> str:
    """Remove HTML tags (incl. script/style content), decode entities, collapse whitespace."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def matches(blob: str, words: list[str]) -> bool:
    """True if any query word appears in ``blob`` (case-insensitive).
    Returns True unconditionally when words is empty (accept all)."""
    if not words:
        return True
    blob = blob.lower()
    return any(w in blob for w in words)


def epoch_to_iso(epoch: object) -> str | None:
    """Convert a Unix epoch (int/str/float) to an ISO 8601 UTC string, or None
    if ``epoch`` isn't a valid timestamp (Arbeitnow's ``created_at`` and
    Himalayas' ``pubDate`` are both raw epoch ints, not ISO strings)."""
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError, OverflowError):
        return None
