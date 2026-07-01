"""Candidate details — the personal facts a job form asks for that the résumé can't
supply (notice period, current/expected CTC, relocation, work authorization, …).

Single source of truth = ``candidate.json`` at the repo root (gitignored — it holds
pay + personal data). A committed ``candidate.example.json`` documents the schema.

Two consumers, both honesty-preserving:
  - humanise-responder feeds the *known* facts into the answer prompt and lists the
    *unknown* ones in ``screening_todo`` (deterministically — never fabricated).
  - apply-agent's packet carries the known facts so the form-filler (orchestrator)
    can answer screening questions directly instead of stopping for every one.

Pure stdlib (json) — no dependency, mirroring the rest of the store layer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DETAILS_PATH = ROOT / "candidate.json"

# The fields a job application commonly asks for, in (key → human label) form. The
# label is what surfaces in screening_todo / the packet so a human reads plain words.
# `kind` decides "is it filled in": text = non-empty string; bool = present at all
# (False is a real, usable answer — e.g. "needs visa sponsorship? No").
FIELDS: dict[str, dict[str, str]] = {
    "notice_period":           {"label": "notice period",            "kind": "text"},
    "current_ctc":             {"label": "current CTC",              "kind": "text"},
    "expected_ctc":            {"label": "expected CTC",             "kind": "text"},
    "total_experience":        {"label": "total experience",        "kind": "text"},
    "willing_to_relocate":     {"label": "willingness to relocate",  "kind": "bool"},
    "preferred_locations":     {"label": "preferred locations",      "kind": "list"},
    "work_authorization":      {"label": "work authorization",       "kind": "text"},
    "visa_sponsorship_needed": {"label": "visa sponsorship need",    "kind": "bool"},
    "availability_to_start":   {"label": "availability to start",    "kind": "text"},
}

# Contact / identity fields (always "known" if set; never go to screening_todo).
CONTACT_FIELDS = ("full_name", "email", "phone", "location", "linkedin", "github",
                  "current_company", "current_title")


def load_details(path: Path | None = None) -> dict[str, Any]:
    """Load candidate.json. Returns {} (not an error) if the file is absent, so the
    pipeline still runs and every screening field simply falls to the human gate."""
    src = path or DETAILS_PATH
    if not src.exists():
        return {}
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_filled(key: str, value: Any) -> bool:
    """Is this field actually answered? text → non-empty after strip; list → non-empty;
    bool → present (any explicit True/False). None/"" → not filled."""
    if value is None:
        return False
    kind = FIELDS.get(key, {}).get("kind", "text")
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "list":
        return isinstance(value, list) and len([v for v in value if str(v).strip()]) > 0
    return bool(str(value).strip())


def known_facts(details: dict[str, Any]) -> dict[str, Any]:
    """The subset of FIELDS + CONTACT_FIELDS that are filled in — safe to use in an
    answer or to type into a form. Excludes empty fields so nothing gets fabricated."""
    out: dict[str, Any] = {}
    for key in (*FIELDS, *CONTACT_FIELDS):
        val = details.get(key)
        if key in FIELDS:
            if _is_filled(key, val):
                out[key] = val
        elif val is not None and str(val).strip():
            out[key] = val
    return out


def screening_gaps(details: dict[str, Any]) -> list[str]:
    """Human-readable labels of the FIELDS still unanswered — these are what a human
    must supply at the apply gate. Deterministic: derived from the file, not the LLM."""
    return [meta["label"] for key, meta in FIELDS.items()
            if not _is_filled(key, details.get(key))]


def covered_labels(facts: dict[str, Any]) -> list[str]:
    """Labels of the screening FIELDS that ARE answered in a ``known_facts`` dict.

    Used at apply time to drop now-answered items from a job's stored ``screening_todo``
    (which was computed earlier, possibly before the user filled candidate.json), so the
    packet's ``human_must_fill`` doesn't list a fact the packet already supplies."""
    return [FIELDS[key]["label"] for key in facts if key in FIELDS]
