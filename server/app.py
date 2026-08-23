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
from execution import llm_health  # noqa: E402
from execution import prompts as prompt_store  # noqa: E402
from execution.eligibility import classify  # noqa: E402
from plugins.registry import discover_plugins  # noqa: E402

app = FastAPI(title="job-search control panel")
app.add_middleware(
    CORSMiddleware,
    # Local dev only — the Vite dev server runs on a different port.
    allow_origins=["http://localhost:5178", "http://127.0.0.1:5178"],
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


# Every status downstream of matching — a job keeps showing in its eligibility
# tier as it moves through the pipeline instead of vanishing once prep advances
# it off "matched" (the CLI's `lists` command intentionally only shows the
# matched-and-undecided queue; the UI additionally wants prepped/applied jobs to
# stay visible with their status, so a run's result is visibly reflected next to
# the job instead of the card just disappearing).
_POST_MATCH_STATUSES = ("matched", "tailored", "applied", "skipped", "failed")


@app.get("/api/lists")
def get_lists() -> dict:
    """Jobs bucketed into the same four eligibility tiers `main.py lists` shows,
    across every post-match status — lets the frontend inform a prep
    job-selection choice with real counts/rows, and keeps showing each job's
    pipeline progress (tailored/applied/...) instead of dropping it once
    prep advances it off 'matched'."""
    jobs = [j for j in store.get_jobs(order="score") if j["status"] in _POST_MATCH_STATUSES]
    tiers: dict[str, list[dict]] = {"eligible": [], "needs_mod": [], "stretch": [], "off_profile": []}
    for j in jobs:
        tiers.setdefault(classify(j), []).append(j)
    return tiers


# ── LLM provider health ──────────────────────────────────────────────────────
# Drives the Rank/Prep panels' LLM dropdown: which provider to preselect and how
# to order/annotate the rest. Probing is blocking network I/O (execution/
# llm_health.py's _probe() does a synchronous urllib completion call per
# candidate) — always run it via asyncio.to_thread so a dead provider's timeout
# can't stall the event loop for every other request. Cheap on a normal page
# load: llm_health caches results to data/llm_health.json for TTL_SECS (10 min).

@app.get("/api/llm-providers")
async def get_llm_providers(force: bool = False) -> dict:
    picked, _ = await asyncio.to_thread(
        llm_health.pick_provider, cli._llm_env, force_recheck=force)
    # pick_provider() stops probing at the first success, so its own results
    # dict can be missing later candidates entirely on a cold cache — use
    # status_table() (cache-only, always full DEFAULT_ORDER) for the rows the
    # UI renders, now warmed by whatever pick_provider just probed above.
    rows = await asyncio.to_thread(llm_health.status_table)
    return {"picked": picked, "providers": rows}


# ── shared subprocess streaming + pipeline lock ─────────────────────────────

# Only one pipeline operation (search/prep/rank) at a time — two concurrent
# runs would interleave SSE output in the CMD panel and race on the same
# store (upserts, _auto_reject, status transitions all assume exclusivity).
_pipeline_lock = asyncio.Lock()


async def run_streamed(script: Path, *args: str, env: dict | None = None):
    """Run `script` as a subprocess, yielding each stdout line as an SSE `line`
    event as it's produced, then a final `exit` event with the return code.
    Shared by every pipeline endpoint (search/prep/rank) — the CMD panel's
    single data source regardless of which stage is running."""
    proc = await asyncio.create_subprocess_exec(
        cli.PY, str(script), *args,
        cwd=str(cli.ROOT),
        # PYTHONUNBUFFERED=1 is the fix for "output only appears at the end":
        # stdout is a pipe here (not a TTY), so Python block-buffers it by
        # default — every print() from this script AND from any nested
        # subprocess it spawns (main.py's `_run()` calls into understand.py/
        # tailor.py/respond.py, inheriting this env) queues up in a buffer and
        # only actually reaches this pipe when the buffer fills or the
        # process exits. Forcing unbuffered mode makes each line arrive as
        # soon as it's printed, all the way down the subprocess chain.
        env={**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # Default asyncio StreamReader limit is 64 KiB per line; a JD/notes dump
        # printed on one line (no embedded newline) can exceed that and raise
        # ValueError, killing the pump mid-run. 1 MiB comfortably covers it.
        limit=1024 * 1024,
    )
    yield_queue: asyncio.Queue = asyncio.Queue()

    async def pump():
        # try/finally: without this, an exception here (e.g. a line over the
        # StreamReader's limit — see below) never queues the `None` sentinel,
        # so the consumer loop below awaits it forever and the SSE stream
        # hangs with no error surfaced to the UI.
        try:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                await yield_queue.put(raw.decode(errors="replace").rstrip("\n"))
        except Exception as exc:  # noqa: BLE001 — surface it as a line, don't just die silently
            await yield_queue.put(f"⚠ output pump error: {type(exc).__name__}: {exc}")
        finally:
            await yield_queue.put(None)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            line = await yield_queue.get()
            if line is None:
                break
            yield {"event": "line", "data": line}
        await pump_task
        rc = await proc.wait()
        yield {"event": "exit", "data": str(rc)}
    finally:
        # If we get here abnormally — the client disconnected (e.g. a page
        # refresh) mid-stream, cancelling this generator — the subprocess
        # would otherwise keep running orphaned in the background with
        # nothing left to consume its output. The pipeline lock releases as
        # soon as this generator unwinds (see _locked_sse), so without this,
        # a new run could start immediately and race the orphaned one on the
        # same SQLite store — exactly what the lock exists to prevent.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        if not pump_task.done():
            pump_task.cancel()


def _locked_sse(gen_factory):
    """Wrap an SSE generator factory with the shared pipeline lock: 409 if
    something is already running, otherwise hold the lock for the stream's
    full duration."""
    if _pipeline_lock.locked():
        raise HTTPException(409, "a pipeline operation is already running — wait for it to finish")

    async def gen():
        async with _pipeline_lock:
            async for evt in gen_factory():
                yield evt
    return EventSourceResponse(gen())


# ── search (SSE) ─────────────────────────────────────────────────────────────

_SEARCH_LLM_CHOICES = ("auto", "grok", "deepseek", "nvidia", "api")


class SearchRequest(BaseModel):
    queries: str
    locations: str = "Hyderabad,Bengaluru,India"
    days: int = 2
    source: str = "all"
    limit: int = 30
    workers: int = 0  # 0 = auto: one worker per available plugin (see scrape.py)
    recheck: bool = False
    llm: str = "auto"  # LLM for the post-scrape rerank step; forcing one skips the probe.


async def _stream_search(req: SearchRequest):
    """Run scrape.py -> match.py -> LLM rerank (auto-picked or forced provider) ->
    auto-reject — full parity with `main.cmd_search`, streamed instead of inheriting
    the terminal directly."""
    env = {"LINKEDIN_POSTED_DAYS": str(req.days)} if req.days else {}

    scrape_args = ["--source", req.source, "--queries", req.queries,
                   "--locations", req.locations, "--limit", str(req.limit),
                   "--workers", str(req.workers)]
    if req.recheck:
        scrape_args.append("--recheck")

    yield {"event": "line", "data": f"$ scrape.py {' '.join(scrape_args)}"}
    async for evt in run_streamed(cli.SCRAPE, *scrape_args, env=env):
        yield evt
    yield {"event": "line", "data": "\n$ match.py"}
    async for evt in run_streamed(cli.MATCH):
        yield evt

    if req.llm == "auto":
        # cli._pick_llm_provider() does blocking urllib network I/O (one probe per
        # candidate provider) — run it off the event loop so SSE keeps flowing for
        # any other client, and emit a heartbeat first since a probe against a dead
        # provider can take several seconds with nothing to print in the meantime.
        yield {"event": "line", "data": "\nprobing LLM providers (nvidia → grok → deepseek → api)…"}
        provider, probe_results = await asyncio.to_thread(cli._pick_llm_provider)
        if provider is None:
            reasons = "; ".join(f"{p}: {r['detail']}" for p, r in probe_results.items())
            yield {"event": "line", "data": f"\n⚠ no working LLM provider ({reasons}) — ordering "
                                             f"by keyword match_score instead."}
    else:
        provider = req.llm

    if provider:
        yield {"event": "line", "data": f"\nLLM rerank via {provider} "
                                         f"({'auto-picked' if req.llm == 'auto' else 'forced'})…"}
        async for evt in run_streamed(cli.LLM_RANK, "--save", env=cli._llm_env(provider)):
            yield evt

    rejected = await asyncio.to_thread(cli._auto_reject)
    if rejected:
        yield {"event": "line", "data": f"\n🚫 auto-rejected {rejected} off-profile job(s)"}
    stats = await asyncio.to_thread(store.stats)
    yield {"event": "done", "data": json.dumps({"stats": stats})}


@app.post("/api/search")
async def post_search(req: SearchRequest):
    if req.llm not in _SEARCH_LLM_CHOICES:
        raise HTTPException(400, f"invalid llm {req.llm!r}; choices: {_SEARCH_LLM_CHOICES}")
    return _locked_sse(lambda: _stream_search(req))


# ── prep (SSE) ───────────────────────────────────────────────────────────────
# Full parity with `main.py prep`'s flags. Rather than reimplementing its
# job-selection/staging logic here, this runs `main.py prep <flags>` itself as
# a single subprocess (via the shared run_streamed) — one source of truth for
# that logic, same as the CLI.

_PREP_LLM_CHOICES = ("auto", "claude", "grok", "deepseek", "nvidia", "api")
_PREP_SELECTIONS = ("pending", "eligible", "needs_mod", "stretch", "llm_best", "jobs")
_PREP_SELECTION_FLAGS = {
    "eligible": "--eligible", "needs_mod": "--needs-mod",
    "stretch": "--stretch", "llm_best": "--llm-best",
}


class PrepRequest(BaseModel):
    llm: str = "auto"
    # needs_mod = jobs that need résumé tailoring — the tier prep actually exists
    # for (2026-08-23, PLAN §9: revises the earlier 'pending' default, which ran
    # every matched job including ones the master résumé already fits as-is).
    selection: str = "needs_mod"
    jobs: str | None = None  # comma-separated ids, required when selection == "jobs"
    modify_resume: bool = False
    limit: int | None = None


async def _stream_prep(args: list[str]):
    yield {"event": "line", "data": f"$ main.py {' '.join(args)}"}
    async for evt in run_streamed(cli.ROOT / "main.py", *args):
        yield evt
    yield {"event": "done", "data": json.dumps({"stats": store.stats()})}


@app.post("/api/prep")
async def post_prep(req: PrepRequest):
    if req.llm not in _PREP_LLM_CHOICES:
        raise HTTPException(400, f"invalid llm {req.llm!r}; choices: {_PREP_LLM_CHOICES}")
    if req.selection not in _PREP_SELECTIONS:
        raise HTTPException(400, f"invalid selection {req.selection!r}; choices: {_PREP_SELECTIONS}")
    if req.selection == "jobs" and not (req.jobs or "").strip():
        raise HTTPException(400, "selection 'jobs' requires a non-empty comma-separated job id list")
    if req.limit is not None and req.limit < 1:
        raise HTTPException(400, "limit must be >= 1")

    args = ["prep", "--llm", req.llm]
    if req.selection in _PREP_SELECTION_FLAGS:
        args.append(_PREP_SELECTION_FLAGS[req.selection])
    elif req.selection == "jobs":
        args += ["--jobs", req.jobs]  # type: ignore[list-item] — validated non-empty above
    if req.modify_resume:
        args.append("--modify-resume")
    if req.limit is not None:
        args += ["--limit", str(req.limit)]

    return _locked_sse(lambda: _stream_prep(args))


# ── rank (SSE) ───────────────────────────────────────────────────────────────

_RANK_LLM_CHOICES = ("auto", "grok", "deepseek", "nvidia", "api")


class RankRequest(BaseModel):
    llm: str = "auto"
    limit: int = 20
    eligible: bool = False
    jobs: str | None = None
    save: bool = False


async def _stream_rank(args: list[str]):
    yield {"event": "line", "data": f"$ main.py {' '.join(args)}"}
    async for evt in run_streamed(cli.ROOT / "main.py", *args):
        yield evt
    yield {"event": "done", "data": json.dumps({"stats": store.stats()})}


@app.post("/api/rank")
async def post_rank(req: RankRequest):
    if req.llm not in _RANK_LLM_CHOICES:
        raise HTTPException(400, f"invalid llm {req.llm!r}; choices: {_RANK_LLM_CHOICES}")
    if req.limit < 1:
        raise HTTPException(400, "limit must be >= 1")

    args = ["rank", "--llm", req.llm, "--limit", str(req.limit)]
    if req.eligible:
        args.append("--eligible")
    if req.jobs:
        args += ["--jobs", req.jobs]
    if req.save:
        args.append("--save")

    return _locked_sse(lambda: _stream_rank(args))


# ── reset ─────────────────────────────────────────────────────────────────

class ResetRequest(BaseModel):
    hard: bool = False


@app.post("/api/reset")
def post_reset(req: ResetRequest) -> dict:
    return store.reset(hard=req.hard)


# ── outcome logging (pure status-recording — never submits anything) ────────
# Wraps apply-agent's `log` subcommand, which itself refuses to log a job that
# isn't at status 'tailored' (the apply gate — no separate 'ready' stage) —
# see apply.py's `log()`. This endpoint does not open a browser, fill a form,
# or click submit; it only records the outcome a human already acted on
# elsewhere.

_LOG_OUTCOMES = ("applied", "skipped", "failed")


class LogRequest(BaseModel):
    job: int
    outcome: str
    note: str | None = None
    # Bypasses apply.py's "job must be at status 'tailored'" guard — for
    # deliberately recording an outcome on a job that never went through this
    # tool's own prep flow (e.g. applied to it elsewhere). Still pure
    # status-recording: never opens a browser, never submits anything: it only
    # overrides which jobs the write is allowed to target.
    force: bool = False


@app.post("/api/log")
def post_log(req: LogRequest) -> dict:
    if req.outcome not in _LOG_OUTCOMES:
        raise HTTPException(400, f"invalid outcome {req.outcome!r}; choices: {_LOG_OUTCOMES}")
    args = [cli.PY, str(cli.APPLY), "log", "--job", str(req.job), "--outcome", req.outcome]
    if req.note:
        args += ["--note", req.note]
    if req.force:
        args.append("--force")
    proc = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True)
    if proc.returncode != 0:
        raise HTTPException(400, (proc.stdout + proc.stderr).strip() or "log failed")
    return {"ok": True, "job": store.get_job(req.job)}


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


@app.get("/api/jobs/{job_id}/resume/pdf")
def get_job_resume_pdf(job_id: int) -> FileResponse:
    """Serve the résumé PDF a specific job's `tailored_resume_path` points at
    (set by resume-tailor once it runs — the master PDF verbatim for
    eligible/as-is jobs, a per-job tailored PDF for needs_mod/stretch). 404 if
    tailor hasn't run for this job yet; the frontend falls back to linking the
    master résumé directly for the eligible tier in that case."""
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"no job with id {job_id}")
    rel = job.get("tailored_resume_path")
    if not rel:
        raise HTTPException(404, f"job {job_id} has no tailored résumé yet")
    path = (ROOT / rel) if not Path(rel).is_absolute() else Path(rel)
    try:
        resolved = path.resolve()
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        raise HTTPException(400, "résumé path is outside the project root") from None
    if not resolved.exists():
        raise HTTPException(404, f"résumé file not found on disk: {rel}")
    return FileResponse(resolved, media_type="application/pdf")
