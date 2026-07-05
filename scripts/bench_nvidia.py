#!/usr/bin/env python3
"""Re-runnable NVIDIA model benchmark for the job-search pipeline.

Tests NVIDIA NIM models against our two heaviest real tasks:
  RANK        — batch job-ranking prompt (temp 0, all-ids, strict JSON)
  UNDERSTAND  — JD-understanding prompt (temp 0.2, structured JSON)

Usage:
  python3 scripts/bench_nvidia.py              # uses NVIDIA_API_KEY from .env
  NVIDIA_API_KEY=nvapi-… python3 scripts/bench_nvidia.py

Output: per-model latency, JSON-OK/FAIL, seniority calibration score, reasoning leak.
The seniority calibration checks whether the model correctly scores a senior-scope role
BELOW the eligibility.LLM_BEST_SCORE (60) gate — the core requirement for the matcher.

Benchmark results as of 2026-07-04 (see PLAN.md §9 for the decision log):
  Primary  moonshotai/kimi-k2.6                    — best calibration (6/6), 1.6/4.7 s
  Backup   mistralai/mistral-large-3-675b-instruct-2512 — fastest (1.3/2.8 s)
  Avoid    meta/llama-3.3-70b-instruct, deepseek-ai/deepseek-v4-pro — 150 s timeouts
  Avoid    nvidia/llama-3.3-nemotron-super-49b-v1.5, nemotron-3-super-120b — blows token budget
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --- env setup ---
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

KEY = os.environ.get("NVIDIA_API_KEY", "")
BASE = os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
URL = f"{BASE}/chat/completions"

if not KEY:
    print("ERROR: NVIDIA_API_KEY not set. Copy .env.example → .env and fill in the key.",
          file=sys.stderr)
    sys.exit(1)

# --- real prompts (matches main.py/llm_rank.py) ---
RANK_SYS = (
    "You are an expert technical recruiter ranking cybersecurity job postings by fit for ONE "
    "candidate (~2 yrs exp; core strength = security AUTOMATION in Python/Bash, vulnerability-"
    "detection development, cloud security/compliance). RULES: (1) Judge from the jd DUTIES, "
    "not the title. (2) An EXPLICIT senior marker ('Senior','Lead','Staff','Principal','Manager',"
    "'VP', or level >=3) signals a seniority gap for a 2-yr candidate: CAP such a role BELOW "
    "on-level matches even when duties fit. (3) Rank pure manual/red-team pentest roles BELOW "
    "automation/cloud/appsec/detection roles. Return STRICT JSON, no prose/fences/<think>: "
    '{"ranking":[{"id":<int>,"score":<0-100 int>,"reason":"<=10 words, cite duties"}]}. '
    "Include EVERY id EXACTLY ONCE, best-fit first."
)
RANK_JOBS = [
    {"id": 1, "title": "Security Automation Engineer",
     "jd": "Build Python automation for vuln scanning, CI/CD security gates, AWS IAM checks. 2-4 yrs."},
    {"id": 2, "title": "Senior SOC Analyst",
     "jd": "Build Sigma detections, tune Splunk SIEM, threat hunting, AWS GuardDuty. 5+ yrs required."},
    {"id": 3, "title": "Senior DevSecOps Engineer",
     "jd": "Lead 3 engineers, own cloud security posture, set strategy, 6+ yrs, Terraform/Python."},
    {"id": 4, "title": "Windows Systems Administrator",
     "jd": "Wintel patching, Active Directory, ITIL release management, helpdesk tickets."},
]
UND_SYS = (
    "You analyze ONE job posting for a cybersecurity candidate (~2 yrs) and return STRICT JSON "
    "(no prose, no markdown, no <think>). Schema: {\"company_summary\":\"\",\"role_summary\":\"\","
    "\"key_tools\":[],\"must_have\":[],\"nice_to_have\":[],\"keywords\":[],\"seniority\":\"\","
    "\"red_flags\":[],\"fit_notes\":\"\"}. "
    "Extract only what the posting states. Ground fit_notes in the candidate's real strengths "
    "(Python/Bash security automation, cloud security, detection development)."
)
UND_JOB = (
    "JD: Security Automation Engineer at a fintech. Build Python tooling for automated vuln "
    "scanning across AWS accounts, integrate security gates into GitLab CI/CD, write detections "
    "for cloud misconfig, partner with SRE. Required: 2-4 yrs, Python, AWS, Terraform. Nice: Splunk, Go."
)

MODELS_TO_TEST = [
    ("moonshotai/kimi-k2.6", ""),
    ("mistralai/mistral-large-3-675b-instruct-2512", ""),
    ("nvidia/llama-3.3-nemotron-super-49b-v1", "detailed thinking off"),
    # Uncomment to recheck failed models:
    # ("meta/llama-3.3-70b-instruct", ""),
    # ("deepseek-ai/deepseek-v4-pro", ""),
]


def call(model: str, system: str, user: str, max_tokens: int,
         temperature: float, timeout: int = 120) -> dict:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": user})
    body = json.dumps({"model": model, "messages": msgs,
                       "max_tokens": max_tokens, "temperature": temperature}).encode()
    req = urllib.request.Request(
        URL, data=body,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    t = time.time()
    try:
        d = json.load(urllib.request.urlopen(req, timeout=timeout))
        dt = time.time() - t
        msg = d["choices"][0]["message"]
        c = (msg.get("content") or "").strip()
        leak = bool(msg.get("reasoning_content"))
        return {"ok": True, "dt": dt, "content": c, "leak": leak,
                "ct": d.get("usage", {}).get("completion_tokens")}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {str(e)[:80]}", "dt": time.time() - t}


def json_ok(c: str) -> bool:
    try:
        s = c[c.find("{"):c.rfind("}") + 1]
        json.loads(s)
        return True
    except Exception:
        return False


def seniority_score(content: str) -> str:
    """Check whether the senior+lead 6yr role (#3) scored below the 60 LLM_BEST_SCORE gate.
    Returns PASS / FAIL / PARSE_ERR."""
    try:
        s = content[content.find("{"):content.rfind("}") + 1]
        rk = json.loads(s)["ranking"]
        score_3 = next(r["score"] for r in rk if r["id"] == 3)
        return f"PASS (#{3}={score_3}, <60 ✅)" if score_3 < 60 else f"FAIL (#{3}={score_3}, should be <60)"
    except Exception:
        return "PARSE_ERR"


# ── RANK task ──────────────────────────────────────────────────────────────
print("=" * 70)
print("TASK: RANK  (temp=0, max_tokens=1200)")
print("=" * 70)
for model, prefix in MODELS_TO_TEST:
    sys_p = (f"{prefix}\n{RANK_SYS}".strip() if prefix else RANK_SYS)
    r = call(model, sys_p, json.dumps({"jobs": RANK_JOBS}), 1200, 0)
    if not r["ok"]:
        print(f"\n{model}\n  FAIL: {r['err']}  ({r['dt']:.1f}s)")
        continue
    jok = json_ok(r["content"])
    calib = seniority_score(r["content"]) if jok else "N/A"
    print(f"\n{model}  [prefix={prefix!r}]")
    print(f"  {r['dt']:5.1f}s  JSON-{'OK' if jok else 'FAIL'}  "
          f"reasoning_leak={r['leak']}  ct={r['ct']}")
    print(f"  seniority-gate: {calib}")
    print(f"  -> {r['content'][:200]!r}")

# ── UNDERSTAND task ─────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("TASK: JD-UNDERSTAND  (temp=0.2, max_tokens=900)")
print("=" * 70)
for model, prefix in MODELS_TO_TEST:
    sys_p = (f"{prefix}\n{UND_SYS}".strip() if prefix else UND_SYS)
    r = call(model, sys_p, UND_JOB, 900, 0.2)
    if not r["ok"]:
        print(f"\n{model}\n  FAIL: {r['err']}  ({r['dt']:.1f}s)")
        continue
    jok = json_ok(r["content"])
    print(f"\n{model}  [prefix={prefix!r}]")
    print(f"  {r['dt']:5.1f}s  JSON-{'OK' if jok else 'FAIL'}  "
          f"reasoning_leak={r['leak']}  ct={r['ct']}")
    print(f"  -> {r['content'][:180]!r}")

print("\n" + "=" * 70)
print("DONE. Decision: primary=moonshotai/kimi-k2.6, "
      "backup=mistralai/mistral-large-3-675b-instruct-2512")
print("See PLAN.md §9 for rationale.")
