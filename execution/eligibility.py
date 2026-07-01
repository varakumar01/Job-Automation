"""Shared job-eligibility classification — the SINGLE source of truth for what counts
as an "eligible" best match vs a "needs_mod" non-best job vs "off_profile".

Used by main.py (lists/prep/report) and profile-matcher/llm_rank.py (--eligible), so the
thresholds and the security-title list never drift between them (PLAN §9). Pure stdlib.
"""

from __future__ import annotations

import json
from typing import Any

# A job is "eligible as-is" when the master résumé already strongly covers it;
# "needs_mod" when tailoring would help; "off_profile" when it isn't really a security
# role / is too weak to bother. Tune here once.
ELIGIBLE_SCORE = 70.0
ELIGIBLE_COVERAGE = 0.60
MIN_PROFILE_SCORE = 45.0

# Grok/LLM rerank score (llm_rank --save) cutoff: at/above = a Grok-approved best job;
# below = a Grok-identified dud (from tuning, real fits scored ~65-98, duds ~10-45).
LLM_BEST_SCORE = 60.0

SEC_TITLE = ("security", "pentest", "penetration", "vulnerabilit", "detection",
             "appsec", "soc", "cyber", "threat", "malware", "red team", "infosec",
             "devsecops", "incident", "grc")


def coverage(job: dict[str, Any]) -> float:
    """The profile-matcher's JD-skill coverage (0–1) from the notes JSON, or 0.0."""
    try:
        return float(json.loads(job.get("notes") or "{}")
                     .get("breakdown", {}).get("coverage", 0.0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0.0


def is_security(job: dict[str, Any]) -> bool:
    t = (job.get("title") or "").lower()
    return any(k in t for k in SEC_TITLE)


def classify(job: dict[str, Any]) -> str:
    """'eligible' (apply with master), 'needs_mod' (tailor first), or 'off_profile'."""
    score = job.get("match_score") or 0.0
    if not is_security(job) or score < MIN_PROFILE_SCORE:
        return "off_profile"
    if score >= ELIGIBLE_SCORE and coverage(job) >= ELIGIBLE_COVERAGE:
        return "eligible"
    return "needs_mod"


# ── Grok/LLM-score filter (the tuned reranker decides, not keyword score) ────

def llm_scored(job: dict[str, Any]) -> bool:
    """Has this job been ranked by the LLM reranker (has an llm_score)?"""
    return job.get("llm_score") is not None


def llm_best(job: dict[str, Any], threshold: float = LLM_BEST_SCORE) -> bool:
    """A Grok-approved best job: ranked AND scored at/above the cutoff."""
    s = job.get("llm_score")
    return s is not None and s >= threshold


def llm_dud(job: dict[str, Any], threshold: float = LLM_BEST_SCORE) -> bool:
    """A Grok-rejected job: ranked, but scored below the cutoff (the reranker saw it and
    said no — e.g. a keyword-eligible patching/VP role). Unranked jobs are NOT duds."""
    s = job.get("llm_score")
    return s is not None and s < threshold
