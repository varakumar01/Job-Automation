"""Shared job-eligibility classification — the SINGLE source of truth for what counts
as an "eligible" best match vs a "needs_mod" non-best job vs "stretch" or "off_profile".

Used by main.py (lists/prep/report) and profile-matcher/llm_rank.py (--eligible), so the
thresholds and the security-title list never drift between them (PLAN §9). Pure stdlib.

Classification tiers (2026-07-04 redesign):
  eligible   — master résumé strongly covers the role; apply as-is. score≥70 & coverage≥0.60.
  needs_mod  — security-on-profile, good fit but needs tailoring. score≥MIN_PROFILE_SCORE.
  stretch    — security-on-profile, low fit BUT not a hard-no (scope gap is soft: title
               only, not actual management / 6+yr duties). Routed to resume-tailor with a
               heavier rewrite; human opts in. Pure-pentest 4yr, Principal-titled IC roles
               land here instead of the trash.
  off_profile— hard-nos ONLY: neither title NOR jd_text reads as security work
               (see is_security — title is the fast path, JD is the fallback for
               vague titles), genuine SCOPE seniority (manages people / sets
               strategy / 6+ yrs stated as requirement), or no concrete requirements.
               Auto-rejected to keep token spend on relevant jobs only.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Thresholds — tune here once.
ELIGIBLE_SCORE = 70.0
ELIGIBLE_COVERAGE = 0.60
MIN_PROFILE_SCORE = 45.0   # below this AND security title = stretch, not off_profile
STRETCH_FLOOR = 20.0        # below this even if security title = off_profile (really off)

# Grok/LLM rerank score (llm_rank --save) cutoff: at/above = a Grok-approved best job;
# below = a Grok-identified dud (from tuning, real fits scored ~65-98, duds ~10-45).
LLM_BEST_SCORE = 60.0

SEC_TITLE = ("security", "pentest", "penetration", "vulnerabilit", "detection",
             "appsec", "soc", "cyber", "threat", "malware", "red team", "infosec",
             "devsecops", "incident", "grc")

# SCOPE seniority markers — these signal a HARD GAP (management, strategy, many years)
# that no résumé rewrite can fix for a 2-yr candidate. Distinct from TITLE seniority
# ("Senior Engineer" is tailorable; "Engineering Manager" is a hard scope gap).
# Checked against title AND jd_text snippets for phrases like "lead a team", "6+ years".
SCOPE_TITLE_KEYWORDS = (
    "manager", "director", "vice president", "head of", "principal engineer",
    "staff engineer", "distinguished", "fellow",
)
# "vp" checked via regex below (word boundary) so "vp " substring doesn't miss "VP" at
# end of a title or incorrectly match "vpn", "evp", etc.
_SCOPE_TITLE_RE = re.compile(r"\bvp\b", re.IGNORECASE)

SCOPE_JD_PHRASES = (
    # manage/lead a team — cover common verb forms (gerund, past tense, plural)
    # allow up to 3 intervening words so "managing security teams" matches too
    r"(?:manage[sd]?|managing) (?:\w+ ){0,3}teams?",
    r"(?:lead|leads|led|leading) (?:\w+ ){0,3}teams?",
    r"people manager",
    # explicit multi-year experience requirements (6+ through 19+)
    r"\b[6-9]\+?\s*years?\s+(?:of\s+)?experience",
    r"\b1[0-9]\+?\s*years?\s+(?:of\s+)?experience",
    # org-chart and strategy scope
    r"direct reports?", r"set[s]? (?:the\s+)?(?:team\s+)?strategy",
)
_SCOPE_JD_RE = re.compile("|".join(SCOPE_JD_PHRASES), re.IGNORECASE)


def coverage(job: dict[str, Any]) -> float:
    """The profile-matcher's JD-skill coverage (0–1) from the notes JSON, or 0.0."""
    try:
        return float(json.loads(job.get("notes") or "{}")
                     .get("breakdown", {}).get("coverage", 0.0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0.0


# Minimum number of DISTINCT SEC_TITLE keywords that must appear in the JD before
# a vague-titled job is rescued from off_profile — a single passing mention (e.g.
# "collaborate with the security team") elsewhere in an unrelated JD shouldn't be
# enough; two-or-more distinct hits is a much stronger signal the role is actually
# security work, not just adjacent to it.
JD_SECURITY_MIN_HITS = 2

# "soc" needs a word-boundary check, not a bare substring: as plain substring it
# fires inside "Associate", "Social", "associated" etc. Verified against the live
# DB (2026-08-22): 129/778 rejected JDs and 14/972 titles hit this collision vs.
# only 43/15 genuine "SOC" (Security Operations Center) mentions — and it had
# already let at least one false positive ("Senior Associate, Regulatory Content
# Engineer", Ideagen) through to `matched`. Every other SEC_TITLE entry is either
# a full word unlikely to collide, or a deliberate stem ("vulnerabilit" for
# vulnerability/vulnerabilities) that needs substring matching — so this check is
# scoped to "soc" only, not applied blanket.
_SOC_RE = re.compile(r"\bsoc\b")


def _kw_hit(keyword: str, text: str) -> bool:
    """One SEC_TITLE keyword against lowercased text, with the 'soc' collision fix."""
    if keyword == "soc":
        return bool(_SOC_RE.search(text))
    return keyword in text


def is_security(job: dict[str, Any]) -> bool:
    """True if the title says security work, OR — when the title doesn't use any
    of the SEC_TITLE words (e.g. 'Site Reliability Engineer, Trust & Safety') —
    the job description clearly does (>=JD_SECURITY_MIN_HITS distinct keyword
    hits). Title-only matching was auto-rejecting on-profile roles with vague
    titles; this keeps the title as the fast path and only falls through to the
    JD when it doesn't already settle the question."""
    t = (job.get("title") or "").lower()
    if any(_kw_hit(k, t) for k in SEC_TITLE):
        return True
    jd = (job.get("jd_text") or "").lower()
    if not jd:
        return False
    hits = sum(1 for k in SEC_TITLE if _kw_hit(k, jd))
    return hits >= JD_SECURITY_MIN_HITS


def has_scope_gap(job: dict[str, Any]) -> bool:
    """True when the job signals a HARD scope/seniority gap that no tailoring can fix:
    management duties, explicit 6+ yr requirements, or strategy-setting scope in the JD.
    This is distinct from title seniority ('Senior Engineer' = soft, tailorable).

    Note: SCOPE_TITLE_KEYWORDS uses substring matching on the lowercased title. The
    intent is to catch common management/org-chart titles (Manager, Director, Head of,
    VP). Full-phrase entries like 'principal engineer' only trigger when that exact
    phrase appears adjacently in the title string.
    """
    title = (job.get("title") or "").lower()
    if any(k in title for k in SCOPE_TITLE_KEYWORDS):
        return True
    if _SCOPE_TITLE_RE.search(title):  # word-boundary VP check
        return True
    jd = (job.get("jd_text") or "")[:4000]  # check first 4 KB; enough for requirements
    if jd and _SCOPE_JD_RE.search(jd):
        return True
    return False


def classify(job: dict[str, Any]) -> str:
    """Classify a matched job into one of four tiers:
    'eligible', 'needs_mod', 'stretch', or 'off_profile'.

    - eligible  : master résumé fits; apply as-is.
    - needs_mod : security role, good fit, tailor first.
    - stretch   : security role, low fit (no HARD scope gap) — apply with a heavy rewrite;
                  human opts in. Examples: pure pentest 4yr (on-profile stretch),
                  Principal-titled but IC duties (tailorable seniority).
    - off_profile: non-security title, genuine scope gap (manages people/6+yr), or score
                  too low even for a stretch. Auto-rejected to save token spend.
    """
    score = job.get("match_score") or 0.0

    # 1. Non-security title is always off_profile.
    if not is_security(job):
        return "off_profile"

    # 2. Hard scope gap (management / 6+ yr / strategy) → off_profile even for security
    #    titles. A "Senior Security Manager" or a role that explicitly says "manage a team
    #    of 8" is not tailorable by a 2-yr IC candidate.
    if has_scope_gap(job):
        return "off_profile"

    # 3. Eligible (apply as-is): strong fit AND good résumé coverage.
    if score >= ELIGIBLE_SCORE and coverage(job) >= ELIGIBLE_COVERAGE:
        return "eligible"

    # 4. Needs modification: on-profile, moderate fit.
    if score >= MIN_PROFILE_SCORE:
        return "needs_mod"

    # 5. Stretch: security title, no scope gap, but low score. Still potentially
    #    applyable with a heavy résumé rewrite — keep it visible instead of trashing it.
    if score >= STRETCH_FLOOR:
        return "stretch"

    # 6. Off profile: score too low even for a stretch (really off-profile for this candidate).
    return "off_profile"


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
