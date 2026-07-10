"""FastAPI backend for the local job-search control panel (`main.py serve`).

Thin HTTP/SSE wrapper over the existing CLI (`main.py`) and skill scripts — no
business logic is duplicated here. Runs LOCALLY ONLY (binds 127.0.0.1 by
default from `main.py serve`): it drives real subprocesses, the filesystem,
and — via the `/api/env` "persist" option — `.env` itself, so it must never be
exposed beyond localhost. See PLAN.md §8 Phase 10 (frontend work).

Session env (`/api/env`): values POSTed without `persist: true` only update
this PROCESS's `os.environ` (so subprocess calls spawned by this server
inherit them) — they vanish when the server restarts, satisfying "env loaded
temporarily per session". `persist: true` additionally rewrites `.env` itself.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from sse_starlette.sse import EventSourceResponse  # noqa: E402

import main as cli  # noqa: E402 — the existing main.py CLI; reuse its paths/helpers
from data import store  # noqa: E402
from execution import prompts as prompt_store  # noqa: E402
from plugins.registry import discover_plugins  # noqa: E402

app = FastAPI(title="job-search control panel")
app.add_middleware(
    CORSMiddleware,
    # Local dev only — the Vite dev server runs on a different port.
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CANDIDATE_PATH = ROOT / "candidate.json"
CANDIDATE_TEMPLATE = ROOT / "candidate.example.json"
RESUME_TEX = ROOT / "varakumar_resume.tex"
RESUME_PDF = ROOT / "varakumar_resume.pdf"
ENV_EXAMPLE = ROOT / ".env.example"

# Keys that look secret — masked on GET, never echoed back in full.
_SECRET_RE = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD)", re.IGNORECASE)


# ── health / jobs / sources / stats ─────────────────────────────────────────

@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/jobs")
def get_jobs(status: str | None = None, limit: int | None = None, order: str = "score") -> list[dict]:
    try:
        return store.get_jobs(status=status, limit=limit, order=order)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/stats")
def get_stats() -> dict:
    return store.stats()


@app.get("/api/sources")
def get_sources() -> list[dict]:
    out = []
    for p in discover_plugins():
        try:
            available = p.is_available()
        except Exception as exc:  # noqa: BLE001 — report, don't crash the endpoint
            available, reason = False, f"is_available() raised: {exc}"
        else:
            reason = None if available else p.availability_detail()
        out.append({
            "name": p.name, "base_url": p.base_url or None, "mechanism": p.mechanism or None,
            "available": available, "reason": reason,
        })
    return out


# ── search (SSE) ─────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    queries: str
    locations: str = "Hyderabad,Bengaluru,India"
    days: int = 7
    source: str = "all"
    limit: int = 30
    workers: int = 8


# Only one search at a time — two concurrent runs would interleave SSE output
# in the CMD panel and race on the same store (_auto_reject + upserts).
_search_lock = asyncio.Lock()


async def _stream_search(req: SearchRequest):
    """Run scrape.py then match.py/auto-reject, yielding each stdout line as an
    SSE event as it's produced — the CMD panel's data source. Mirrors
    `main.cmd_search` but streams instead of inheriting the terminal directly."""
    env = {"LINKEDIN_POSTED_DAYS": str(req.days)} if req.days else {}

    async def run_streamed(script: Path, *args: str) -> int:
        proc = await asyncio.create_subprocess_exec(
            cli.PY, str(script), *args,
            cwd=str(cli.ROOT), env={**os.environ, **env},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        yield_queue: asyncio.Queue = asyncio.Queue()

        async def pump():
            assert proc.stdout is not None
            async for raw in proc.stdout:
                await yield_queue.put(raw.decode(errors="replace").rstrip("\n"))
            await yield_queue.put(None)

        pump_task = asyncio.create_task(pump())
        while True:
            line = await yield_queue.get()
            if line is None:
                break
            yield {"event": "line", "data": line}
        await pump_task
        rc = await proc.wait()
        yield {"event": "exit", "data": str(rc)}

    yield {"event": "line", "data": f"$ scrape.py --source {req.source} --queries "
                                     f"{req.queries!r} --locations {req.locations!r} "
                                     f"--limit {req.limit} --workers {req.workers}"}
    async for evt in run_streamed(cli.SCRAPE, "--source", req.source, "--queries", req.queries,
                                  "--locations", req.locations, "--limit", str(req.limit),
                                  "--workers", str(req.workers)):
        yield evt
    yield {"event": "line", "data": "\n$ match.py"}
    async for evt in run_streamed(cli.MATCH):
        yield evt

    rejected = cli._auto_reject()
    if rejected:
        yield {"event": "line", "data": f"\n🚫 auto-rejected {rejected} off-profile job(s)"}
    yield {"event": "done", "data": json.dumps({"stats": store.stats()})}


@app.post("/api/search")
async def post_search(req: SearchRequest):
    if _search_lock.locked():
        raise HTTPException(409, "a search is already running — wait for it to finish")

    async def gen():
        async with _search_lock:
            async for evt in _stream_search(req):
                yield evt
    return EventSourceResponse(gen())


# ── reset ─────────────────────────────────────────────────────────────────

class ResetRequest(BaseModel):
    hard: bool = False


@app.post("/api/reset")
def post_reset(req: ResetRequest) -> dict:
    return store.reset(hard=req.hard)


# ── session env ──────────────────────────────────────────────────────────────

def _known_env_keys() -> list[str]:
    """Every KEY= this project recognizes, parsed from .env.example so the list
    can't drift out of sync with what's actually documented there. Intentionally
    strips a leading '#' before matching — most optional keys in .env.example
    are commented out (e.g. `# APIFY_TOKEN_1=`) but are still real, documented,
    settable keys, not noise to skip."""
    if not ENV_EXAMPLE.exists():
        return []
    keys = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if m:
            keys.append(m.group(1))
    return sorted(set(keys))


def _mask(key: str, value: str) -> str:
    if not value:
        return ""
    if not _SECRET_RE.search(key):
        return value
    return f"…{value[-4:]}" if len(value) > 4 else "…"


@app.get("/api/env")
def get_env() -> dict[str, dict]:
    # "set" means present in os.environ at all — bool(value) would misreport
    # a key deliberately set to "" as unset.
    return {
        k: {"set": k in os.environ, "value": _mask(k, os.environ.get(k, ""))}
        for k in _known_env_keys()
    }


class EnvUpdate(BaseModel):
    key: str
    value: str
    persist: bool = False


@app.post("/api/env")
def post_env(req: EnvUpdate) -> dict:
    key = req.key.strip()
    if not re.match(r"^[A-Z][A-Z0-9_]*$", key):
        raise HTTPException(400, "invalid env key")
    if "\n" in req.value or "\r" in req.value:
        # A newline would inject extra raw lines into .env on persist (and is
        # never meaningful for a single KEY=VALUE entry either way).
        raise HTTPException(400, "env value cannot contain a newline")
    os.environ[key] = req.value  # session-only: this process (and its subprocess children) only
    if req.persist:
        _persist_env_var(key, req.value)
    return {"ok": True, "persisted": req.persist}


_env_write_lock = threading.Lock()


def _persist_env_var(key: str, value: str) -> None:
    """Rewrite KEY=... in .env, appending if not present. Preserves every other
    line untouched. Writes to a temp file + atomic rename so a crash mid-write
    (or a concurrent call) can't leave .env truncated or corrupted — this file
    holds real secrets, losing it is expensive for the user to recover from."""
    with _env_write_lock:
        env_path = ROOT / ".env"
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        pattern = re.compile(rf"^{re.escape(key)}=")
        found = False
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = f"{key}={value}"
                found = True
                break
        if not found:
            lines.append(f"{key}={value}")
        tmp_path = env_path.parent / (env_path.name + ".tmp")
        tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp_path.replace(env_path)


# ── prompts ──────────────────────────────────────────────────────────────────

_SKILL_SCRIPTS = {
    "jd-understander": ROOT / ".claude/skills/jd-understander/scripts/understand.py",
    "humanise-responder": ROOT / ".claude/skills/humanise-responder/scripts/respond.py",
    "profile-matcher": ROOT / ".claude/skills/profile-matcher/scripts/llm_rank.py",
}


_default_prompt_cache: dict[str, str] = {}


def _skill_default_prompt(name: str) -> str:
    """The skill script's built-in _DEFAULT_SYSTEM_PROMPT constant — a source
    literal that never changes at runtime, so it's read (importing the script
    file just far enough to see the constant, without re-running its CLI,
    guarded by `if __name__ == "__main__"` in every skill script) once per
    server process and cached, instead of re-executing the whole script
    (with all its imports/dotenv-loading/sys.path mutation) on every GET."""
    if name not in _default_prompt_cache:
        path = _SKILL_SCRIPTS[name]
        spec = importlib.util.spec_from_file_location(f"_prompt_default_{name}", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)
        _default_prompt_cache[name] = getattr(mod, "_DEFAULT_SYSTEM_PROMPT", "")
    return _default_prompt_cache[name]


@app.get("/api/prompts")
def get_prompts() -> dict[str, dict]:
    out = {}
    for name in prompt_store.PROMPT_NAMES:
        override_path = prompt_store.PROMPTS_DIR / f"{name}.txt"
        is_default = not override_path.exists()
        text = _skill_default_prompt(name) if is_default else override_path.read_text(encoding="utf-8")
        out[name] = {"text": text, "is_default": is_default}
    return out


class PromptUpdate(BaseModel):
    text: str


@app.post("/api/prompts/{name}")
def post_prompt(name: str, req: PromptUpdate) -> dict:
    try:
        prompt_store.save_prompt(name, req.text)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


# ── profile (candidate.json) ─────────────────────────────────────────────────

@app.get("/api/profile")
def get_profile() -> dict:
    path = CANDIDATE_PATH if CANDIDATE_PATH.exists() else CANDIDATE_TEMPLATE
    if not path.exists():
        raise HTTPException(404, "no candidate.json or candidate.example.json found")
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/profile")
def post_profile(body: dict[str, Any]) -> dict:
    CANDIDATE_PATH.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"ok": True}


# ── résumé (.tex, compile, PDF preview) ─────────────────────────────────────

@app.get("/api/resume")
def get_resume() -> dict:
    if not RESUME_TEX.exists():
        raise HTTPException(404, "varakumar_resume.tex not found")
    return {"tex": RESUME_TEX.read_text(encoding="utf-8"), "pdf_exists": RESUME_PDF.exists()}


class ResumeUpdate(BaseModel):
    tex: str


@app.post("/api/resume")
def post_resume(req: ResumeUpdate) -> dict:
    RESUME_TEX.write_text(req.tex, encoding="utf-8")
    try:
        proc = subprocess.run(
            ["tectonic", str(RESUME_TEX)], cwd=str(ROOT),
            capture_output=True, text=True, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": proc.returncode == 0, "stderr": proc.stderr[-4000:], "pdf_exists": RESUME_PDF.exists()}


@app.get("/api/resume/pdf")
def get_resume_pdf() -> FileResponse:
    if not RESUME_PDF.exists():
        raise HTTPException(404, "PDF not built yet — save the résumé to compile it")
    return FileResponse(RESUME_PDF, media_type="application/pdf")
