"""Shared candidate profile — parsed ONCE from the master résumé + candidate.json.

The single source of the candidate's summary / skills / target-roles / experience used
by the LLM skills (llm_rank ranking, jd-understander fit_notes, …) so a fit angle can be
CANDIDATE-SPECIFIC rather than generic, and the parsing isn't duplicated per skill.
Pure stdlib + execution.candidate. [PLAN §5]
"""

from __future__ import annotations

import re
from pathlib import Path

from execution import candidate

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "varakumar_resume.tex"


def clean_tex(s: str) -> str:
    s = s.replace("\\&", "&").replace("\\%", "%").replace("{,}", ",")
    s = re.sub(r"\$[^$]*\$", "", s)
    s = re.sub(r"\\textbf\b", "", s)
    s = re.sub(r"\\[a-zA-Z]+\b\*?", " ", s)
    s = re.sub(r"[{}\\]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def candidate_profile(master_path: Path | None = None) -> dict:
    """Résumé-derived profile: current title, experience, target roles, preferred
    locations, professional summary, and skill lines."""
    master = master_path or MASTER
    raw = master.read_text(encoding="utf-8") if master.exists() else ""
    raw = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("%"))
    summary = ""
    m = re.search(r"\\resumeSection\{Professional Summary\}\n(.*?)(?=\n%\s*──|\n\\resumeSection)",
                  raw, re.DOTALL)
    if m:
        summary = clean_tex(m.group(1))
    skills = []
    for ln in raw.splitlines():
        mt = re.match(r"\\techrow\{([^}]*)\}\{([^}]*)\}", ln)
        if mt:
            skills.append(clean_tex(mt.group(2)))
    # Target roles from the résumé tagline — the candidate's strongest-fit areas.
    roles: list[str] = []
    mt = re.search(r"\\itshape\s+(.*?)\}", raw)
    if mt:
        roles = [r for r in (clean_tex(p) for p in re.split(r"\\textbar", mt.group(1))) if r]
    details = candidate.load_details()
    exp = details.get("total_experience") or "~2 years"
    return {
        "current_title": details.get("current_title") or "Security Developer",
        "total_experience": exp,
        "experience_level": f"junior-to-mid ({exp}) — NOT senior/lead/staff/principal/manager",
        "target_roles": roles,
        "preferred_locations": details.get("preferred_locations") or [],
        "summary": summary[:600],
        "skills": [s for s in skills if s][:14],
    }
