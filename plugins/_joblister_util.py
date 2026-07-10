"""Shared helpers for public-JSON joblister plugins (no auth required).

Factored out of the original inline ``remoteok.py`` implementation so the five
sibling plugins (remotive, arbeitnow, jobicy, himalayas, themuse) don't each
duplicate HTML-stripping and keyword-matching. Leading underscore keeps the
registry from treating this file as a plugin (registry.py skips ``_*``).
"""

from __future__ import annotations

import html
import html.entities
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# XML only predefines these 5 named entities; anything else is technically
# invalid XML even though it's valid HTML. Some feeds (e.g. NoDesk) embed raw
# HTML entities like &rsquo;/&ndash; in <description> without CDATA-wrapping
# them, which makes ET.fromstring reject the whole document as malformed.
_XML_PREDEFINED_ENTITIES = {"amp;", "lt;", "gt;", "apos;", "quot;"}
_ENTITY_RE = re.compile(r"&(#?\w+;)")

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


def _local_tag(tag: str) -> str:
    """Strip a namespace prefix off an ElementTree tag: '{ns}creator' -> 'creator'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(elem: ET.Element, *names: str) -> str | None:
    """Text of the first direct child matching ``names``, tried in priority
    order (NOT document order) — e.g. ``_child_text(el, "encoded",
    "description")`` prefers a fuller ``content:encoded`` body over a plain
    ``description`` even when ``description`` appears earlier in the item."""
    for name in names:
        for child in elem:
            if _local_tag(child.tag) == name:
                text = (child.text or "").strip()
                if text:
                    return text
                break  # tag present but empty — don't fall through to a later *same-priority* dup
    return None


def _repair_html_entities(xml_text: str) -> str:
    """Replace non-XML-standard named HTML entities (``&rsquo;``, ``&ndash;``,
    ``&hellip;``, ...) with their literal Unicode characters so
    ``ET.fromstring`` doesn't reject an otherwise well-formed feed. Numeric
    character references (``&#8217;``) are always valid XML and left
    untouched; so are the 5 XML-predefined named entities (unescaping
    ``&amp;`` here would corrupt the document's actual tag structure, not
    just its text content). An unrecognized named entity is left as-is
    (best-effort — ``ET.fromstring`` will surface it as a ``ParseError`` if
    it truly can't be parsed, same as before this repair step existed)."""

    def _sub(m: re.Match) -> str:
        name = m.group(1)
        if name in _XML_PREDEFINED_ENTITIES or name.startswith("#"):
            return m.group(0)
        return html.entities.html5.get(name, m.group(0))

    return _ENTITY_RE.sub(_sub, xml_text)


def parse_feed(xml_text: str) -> list[dict]:
    """Parse an RSS 2.0 or Atom 1.0 feed into a normalized list of dicts.

    Handles both formats (auto-detected per entry: RSS ``<item>`` vs Atom
    ``<entry>``) so lister plugins don't need to know which flavor a given
    board serves. Namespace-tolerant — ``content:encoded`` and ``dc:creator``
    resolve to their local names (``encoded``/``creator``) regardless of the
    namespace prefix the feed declares. Returns ``[]`` on unparsable XML
    rather than raising (mirrors every other parser in this module).

    Each returned dict has: ``title``, ``link``, ``description`` (RSS
    ``content:encoded``/``description`` or Atom ``content``/``summary``),
    ``pubDate`` (RSS ``pubDate`` or Atom ``updated``/``published``), ``guid``
    (RSS ``guid`` or Atom ``id``), ``category``, ``author`` (RSS
    ``dc:creator`` or Atom ``author/name``). Any field not present in the
    source feed is ``None`` — callers should not assume all keys are populated.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        try:
            root = ET.fromstring(_repair_html_entities(xml_text))
        except ET.ParseError:
            return []

    out: list[dict] = []
    for el in root.iter():
        tag = _local_tag(el.tag)
        if tag == "item":
            category = None
            for child in el:
                if _local_tag(child.tag) == "category":
                    category = (child.text or "").strip() or None
                    if category:
                        break
            out.append(
                {
                    "title": _child_text(el, "title"),
                    "link": _child_text(el, "link"),
                    "description": _child_text(el, "encoded", "description"),
                    "pubDate": _child_text(el, "pubDate"),
                    "guid": _child_text(el, "guid"),
                    "category": category,
                    "author": _child_text(el, "creator"),
                }
            )
        elif tag == "entry":
            link = None
            for child in el:
                if _local_tag(child.tag) == "link":
                    href = child.get("href")
                    rel = child.get("rel", "alternate")
                    if href and (link is None or rel == "alternate"):
                        link = href
                        if rel == "alternate":
                            break
            category = None
            for child in el:
                if _local_tag(child.tag) == "category":
                    category = child.get("term") or (child.text or "").strip() or None
                    if category:
                        break
            author = None
            for child in el:
                if _local_tag(child.tag) == "author":
                    author = _child_text(child, "name")
                    if author:
                        break
            out.append(
                {
                    "title": _child_text(el, "title"),
                    "link": link,
                    "description": _child_text(el, "content", "summary"),
                    "pubDate": _child_text(el, "updated", "published"),
                    "guid": _child_text(el, "id"),
                    "category": category,
                    "author": author,
                }
            )
    return out
