"""profile-matcher — rank scraped jobs by how few résumé edits they need. PLAN §5 #2.

Deterministic (pure stdlib, no LLM/API/network). Reads jobs at status ``scraped``
plus the master résumé `.tex`, scores each 0–100 on skill overlap + role fit +
title/seniority fit, picks the best-fit role-profile (the README variants), writes
``match_score`` / ``role_profile`` / a compact rationale in ``notes``, and advances
``scraped → matched``. Persists per job (resumable).

Usage::

    python3 .claude/skills/profile-matcher/scripts/match.py            # score all scraped
    python3 .claude/skills/profile-matcher/scripts/match.py --dry-run  # compute + print, no writes
    python3 .claude/skills/profile-matcher/scripts/match.py --show     # ranked matched jobs
    python3 .claude/skills/profile-matcher/scripts/match.py --resume path/to/resume.tex
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
ROOT = SKILL_DIR.parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data import store  # noqa: E402

DEFAULT_RESUME = ROOT / "varakumar_resume.tex"

# Role-profiles (README §"Creating Role-Specific Variants"). Each maps to a résumé
# variant resume-tailor will produce; keywords drive both role pick and the vocab.
ROLE_PROFILES: dict[str, dict] = {
    "red_team": {
        "label": "Red Team / Pentest",
        "keywords": ["red team", "penetration testing", "pentest", "offensive security",
                     "offensive", "exploitation", "exploit", "burp suite", "metasploit",
                     "oscp", "active directory", "osint", "social engineering",
                     "privilege escalation", "nmap", "kali", "c2", "adversary emulation"],
    },
    "detection_eng": {
        "label": "Detection Engineering",
        "keywords": ["detection engineering", "detection rule", "detection", "nasl", "nse",
                     "signature", "siem", "threat detection", "openvas", "nessus", "qualys",
                     "vulnerability detection", "mitre att&ck", "yara", "sigma", "splunk",
                     "elastic", "sentinel", "edr"],
    },
    "cloud_sec": {
        "label": "Cloud Security",
        "keywords": ["cloud security", "aws", "azure", "gcp", "cloudsploit", "cis benchmark",
                     "cspm", "iam", "kubernetes", "container", "terraform", "compliance",
                     "cloud"],
    },
    "ics_ot": {
        "label": "ICS/OT Security",
        "keywords": ["ics", "scada", "ot security", "operational technology", "modbus",
                     "bacnet", "dnp3", "profinet", "ethernet/ip", "plc", "purdue"],
    },
    "vuln_research": {
        "label": "Vulnerability Research",
        "keywords": ["vulnerability research", "cve", "exploit development", "fuzzing",
                     "reverse engineering", "binary analysis", "nvd", "zero-day", "0-day",
                     "reversing"],
    },
    "appsec": {
        "label": "Application Security",
        "keywords": ["application security", "appsec", "owasp", "sast", "dast",
                     "secure code review", "secure sdlc", "api security", "code review",
                     "threat modeling"],
    },
}

# Common security skills the recognizer should know beyond the candidate's résumé,
# so "missing" skills (in JD, not in résumé) are surfaced for resume-tailor.
EXTRA_VOCAB = [
    "python", "bash", "node.js", "golang", "java", "c++", "powershell", "sql",
    "docker", "ci/cd", "linux", "firewall", "ids", "ips", "vpn", "pki",
    "encryption", "incident response", "threat hunting", "soc", "soc 2",
    "iso 27001", "gdpr", "nist", "pci dss", "crowdstrike", "wireshark",
    "vulnerability management", "vulnerability assessment", "network security",
    "endpoint security", "malware analysis", "git", "rest api",
]

# Target-role terms (in a job TITLE → likely a fit) and seniority markers (the
# candidate has ~2 yrs, so senior/lead/staff/principal titles fit poorly).
TITLE_ROLE_TERMS = ["security", "detection", "vulnerability", "pentest", "penetration",
                    "offensive", "appsec", "application security", "cloud security",
                    "soc", "threat", "malware", "red team", "ics", "ot", "scada",
                    "infosec", "cyber"]
SENIORITY_SENIOR = ["senior", "sr.", "sr", "lead", "principal", "staff", "manager",
                    "director", "head of", "head", "vp", "architect"]

SKILL_MAX, ROLE_MAX = 60.0, 25.0


def _clean_tex(raw: str) -> str:
    """Strip LaTeX comments/commands so the résumé text scans like prose."""
    lines = [ln for ln in raw.splitlines() if not ln.lstrip().startswith("%")]
    text = "\n".join(lines)
    # Unescape LaTeX special chars BEFORE stripping backslashes, else `\&` becomes
    # " &" and "mitre att&ck" (vocab term) can't match "MITRE ATT &CK". Mirrors
    # _parse_techrow_skills. Covers the escapes that appear in skill names.
    for esc, lit in (("\\&", "&"), ("\\#", "#"), ("\\_", "_"), ("\\%", "%"), ("\\$", "$")):
        text = text.replace(esc, lit)
    text = re.sub(r"\\[a-zA-Z]+\b", " ", text)   # drop \commands
    text = re.sub(r"[{}\\]", " ", text)          # drop braces/backslashes
    return text


def _parse_techrow_skills(raw_tex: str) -> list[str]:
    """Candidate's explicitly-listed skills from `\\techrow{label}{items}` rows."""
    # Drop LaTeX comment lines first so commented-out/example techrows (e.g. the
    # macro doc line) don't inject spurious vocab like "comma-sep items".
    raw_tex = "\n".join(ln for ln in raw_tex.splitlines() if not ln.lstrip().startswith("%"))
    skills: list[str] = []
    for items in re.findall(r"\\techrow\{[^}]*\}\{([^}]*)\}", raw_tex):
        cleaned = re.sub(r"\\[a-zA-Z]+\b", " ", items)
        cleaned = cleaned.replace("\\&", "&").replace("{", " ").replace("}", " ")
        for part in cleaned.split(","):
            for chunk in re.split(r"\s+/\s+", part):   # split "Bash / iptables", keep EtherNet/IP
                term = chunk.strip().strip(".").lower()
                term = re.sub(r"\s+", " ", term)
                if len(term) >= 2 and not term.startswith("("):
                    skills.append(term)
    return skills


def build_vocab(raw_tex: str) -> list[str]:
    """Recognized-skill vocabulary: role keywords + résumé skills + common extras."""
    vocab: set[str] = set(EXTRA_VOCAB)
    for prof in ROLE_PROFILES.values():
        vocab.update(prof["keywords"])
    vocab.update(_parse_techrow_skills(raw_tex))
    # Longest first so a phrase like "cloud security" is preferred in reporting.
    return sorted(vocab, key=lambda t: (-len(t), t))


@lru_cache(maxsize=4096)
def _pattern(term: str) -> re.Pattern:
    # Word-boundary-ish match that tolerates symbols (ethernet/ip, c++, mitre att&ck),
    # so short tokens (aws, ot, soc) don't match inside other words (draws, robot).
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)


def _present(text: str, term: str) -> bool:
    return bool(_pattern(term).search(text))


def skills_in(text: str, vocab: list[str]) -> list[str]:
    return [t for t in vocab if _present(text, t)]


def score_job(title: str, jd_text: str, resume_skills: set[str], vocab: list[str]) -> dict:
    """Return score breakdown + role pick + matched/missing skills for one job."""
    blob = f"{title}\n{jd_text}".lower()
    jd_skills = skills_in(blob, vocab)
    matched = [s for s in jd_skills if s in resume_skills]
    missing = [s for s in jd_skills if s not in resume_skills]

    coverage = (len(matched) / len(jd_skills)) if jd_skills else 0.0
    abs_bonus = min(len(matched) / 8.0, 1.0)
    skill_score = (0.7 * coverage + 0.3 * abs_bonus) * SKILL_MAX

    role_key, role_label, best_hits = "general", "General Security", 0
    for key, prof in ROLE_PROFILES.items():
        hits = sum(1 for kw in prof["keywords"] if _present(blob, kw))
        if hits > best_hits:
            role_key, role_label, best_hits = key, prof["label"], hits
    role_score = min(best_hits / 5.0, 1.0) * ROLE_MAX

    title_l = title.lower()
    role_in_title = any(_present(title_l, t.strip()) for t in TITLE_ROLE_TERMS)
    # Word-boundary match (not substring) so "head"/"sr" don't fire inside
    # "headless"/"disaster" etc. (matches role_in_title's matching above).
    is_senior = any(_present(title_l, s.strip()) for s in SENIORITY_SENIOR)
    title_score = (12 if role_in_title else 0) + (3 if not is_senior else 0) - (10 if is_senior else 0)

    total = max(0.0, min(100.0, skill_score + role_score + title_score))
    return {
        "score": round(total, 1),
        "role_key": role_key,
        "role_label": role_label,
        "matched": matched,
        "missing": missing,
        "breakdown": {
            "skill": round(skill_score, 1),
            "role": round(role_score, 1),
            "title": title_score,
            "coverage": round(coverage, 2),
        },
    }


def _load_resume(path: Path) -> tuple[str, set[str], list[str]]:
    raw = path.read_text(encoding="utf-8")
    vocab = build_vocab(raw)
    resume_skills = set(skills_in(_clean_tex(raw), vocab))
    return raw, resume_skills, vocab


def run(resume_path: Path, dry_run: bool, rescore: bool = False) -> int:
    if not resume_path.exists():
        print(f"résumé not found: {resume_path}", file=sys.stderr)
        return 1
    _, resume_skills, vocab = _load_resume(resume_path)
    print(f"résumé skills recognized: {len(resume_skills)} | vocab size: {len(vocab)}")

    # Normal: score new `scraped` jobs and advance them. --rescore: recompute
    # already-`matched` jobs in place (after a résumé edit or a scoring fix),
    # leaving status untouched so downstream stages aren't disturbed.
    src_status = "matched" if rescore else "scraped"
    jobs = store.get_jobs(status=src_status)
    if not jobs:
        print(f"no jobs at status {src_status!r} to {'rescore' if rescore else 'match'}.")
        return 0

    results = []
    for job in jobs:
        res = score_job(job.get("title") or "", job.get("jd_text") or "", resume_skills, vocab)
        results.append((job, res))
        if not dry_run:
            notes = json.dumps({
                "role": res["role_label"],
                "breakdown": res["breakdown"],
                "matched": res["matched"][:12],
                "missing": res["missing"][:12],
            }, ensure_ascii=False)
            fields = dict(match_score=res["score"], role_profile=res["role_label"], notes=notes)
            if not rescore:
                fields["status"] = "matched"  # advance; rescore leaves status as-is
            store.update_job(job["id"], **fields)

    results.sort(key=lambda r: r[1]["score"], reverse=True)
    print(f"\n{'scored' if dry_run else 'matched'} {len(results)} job(s) "
          f"({'DRY-RUN, nothing written' if dry_run else 'status → matched'}):\n")
    for job, res in results:
        print(f"  {res['score']:>5.1f}  [{res['role_label']:<22}] "
              f"{(job.get('title') or '')[:42]:<42} @ {(job.get('company') or '')[:22]} ({job['source']})")
    if not dry_run:
        out = store.export_json()
        print(f"\n✓ exported → {out}")
        print(f"  stats: {store.stats()}")
    return 0


def show() -> int:
    jobs = store.get_jobs(status="matched")
    jobs.sort(key=lambda j: (j.get("match_score") or 0), reverse=True)
    if not jobs:
        print("no matched jobs yet — run the matcher first.")
        return 0
    print(f"{len(jobs)} matched job(s), best fit first:\n")
    for j in jobs:
        print(f"  {(j.get('match_score') or 0):>5.1f}  [{(j.get('role_profile') or '?'):<22}] "
              f"{(j.get('title') or '')[:42]:<42} @ {(j.get('company') or '')[:22]} ({j['source']})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Score scraped jobs vs the master résumé")
    ap.add_argument("--resume", default=str(DEFAULT_RESUME), help="master résumé .tex")
    ap.add_argument("--dry-run", action="store_true", help="compute + print, no DB writes")
    ap.add_argument("--show", action="store_true", help="print current matched ranking and exit")
    ap.add_argument("--rescore", action="store_true",
                    help="recompute already-matched jobs in place (no status change)")
    args = ap.parse_args(argv)

    store.init_db()
    if args.show:
        return show()
    return run(Path(args.resume), args.dry_run, rescore=args.rescore)


if __name__ == "__main__":
    raise SystemExit(main())
