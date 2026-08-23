"""Auto-picks a working LLM provider by probing each candidate live.

main.py used to require `--llm nvidia|grok|deepseek|api` to be chosen by hand.
That breaks silently the moment an account's free-tier credit, key, or model
entitlement changes — which happens often (see main.py's NVIDIA_MODEL comment,
revised 2026-08-22 after both prior model picks had gone dead: one 404
"not entitled", one 410 "end of life"). This module tries each candidate with
a one-line completion and returns the first that actually answers, so the
pipeline degrades to whichever provider currently works instead of failing on
a stale default.

State is cached to ``data/llm_health.json`` (same on-disk-JSON, masked-secret
pattern as ``execution/keypool.py`` / ``plugins/_apify_keys.py``) so a single
pipeline run (search -> match -> rank) doesn't re-probe every candidate on
every call, and so ``main.py keys --llm`` can show why the last pick landed
where it did. Unlike KeyPool (which rotates several *interchangeable* keys for
ONE API), this tracks *different providers* with different base URLs/models —
env-var mapping stays main.py's job (``_llm_env``), passed in as ``env_for``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = Path(os.environ.get("LLM_HEALTH_STATE") or (ROOT / "data" / "llm_health.json"))

# Re-probe a provider at most this often — a provider that failed 30s ago is
# still almost certainly broken; a live account shouldn't need re-checking on
# every single call within one pipeline run.
TTL_SECS = 600

DEFAULT_ORDER = ("nvidia", "grok", "deepseek", "api")

_PROBE_PROMPT = "Reply with the single word OK."

# Belt-and-braces (code-reviewer MAJOR, 2026-08-23): _probe() patches the
# process-wide os.environ for the duration of its network call. The server
# runs pick_provider() via asyncio.to_thread for each of the (now 3) UI
# pickers' health requests — without serializing, two concurrent probes for
# different providers can interleave their environ patch/restore and send one
# provider's key to another's base URL. The client dedupes concurrent requests
# (web/src/lib/useLlmProviders.ts), which is the primary fix; this lock makes
# the module itself safe even if some other future caller doesn't dedupe.
_env_lock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _probe(provider: str, env_for: Callable[[str], dict]) -> tuple[bool, str]:
    """One cheap completion under `provider`'s env, run in-process. Patches
    os.environ for the duration only (subprocess children spawned elsewhere —
    e.g. via main.py's _run() — build their own env from _llm_env() directly,
    so this patch never leaks into them)."""
    with _env_lock:
        env = env_for(provider)
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update({k: v for k, v in env.items() if v is not None})
        try:
            # execution.llm reads LLM_PROVIDER/XAI_* etc. at call time (os.environ.get
            # inside each function), not at import time, so no module reload is needed
            # between probes — each _probe() call sees the env this call just set.
            from execution import llm as _llm
            _llm.complete(_PROBE_PROMPT, max_tokens=8)
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 — this function's whole job is to classify failures
            return False, f"{type(exc).__name__}: {exc}"[:300]
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def pick_provider(
    env_for: Callable[[str], dict],
    order: tuple[str, ...] = DEFAULT_ORDER,
    *,
    force_recheck: bool = False,
) -> tuple[str | None, dict[str, dict]]:
    """Try providers in `order`; return (first working name or None, results).

    `results` covers every candidate tried (including cache hits) so a caller
    can report exactly why each one failed, not just that "no provider worked".
    Stops at the first success — later candidates in `order` are left
    unprobed for this call (their last-known state, if any, stays in the
    cache untouched).
    """
    state = _load()
    results: dict[str, dict] = {}
    now = time.time()
    for name in order:
        cached = state.get(name)
        if not force_recheck and cached and (now - cached.get("checked_at_epoch", 0)) < TTL_SECS:
            rec = cached
        else:
            ok, detail = _probe(name, env_for)
            rec = {"ok": ok, "detail": detail, "checked_at_epoch": now,
                   "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
            state[name] = rec
        results[name] = rec
        if rec["ok"]:
            _save(state)
            return name, results
    _save(state)
    return None, results


def status_table(order: tuple[str, ...] = DEFAULT_ORDER) -> list[dict]:
    """Last-known health for every provider (cache only — does not probe)."""
    state = _load()
    rows = []
    for name in order:
        rec = state.get(name, {})
        rows.append({"provider": name, "ok": rec.get("ok"), "detail": rec.get("detail", "—"),
                     "checked_at": rec.get("checked_at", "—")})
    return rows


if __name__ == "__main__":  # smoke test: python3 execution/llm_health.py
    sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    import main as _main  # noqa: E402 — reuse the real _llm_env mapping

    picked, res = pick_provider(_main._llm_env, force_recheck=True)
    for prov, rec in res.items():
        glyph = "✓" if rec["ok"] else "✗"
        print(f"  {glyph} {prov:<9} {rec['detail']}")
    print(f"\npicked: {picked or '(none — every provider failed)'}")
