"""Multi-mode LLM access for pipeline skills. See PLAN.md §9 (LLM_PROVIDER).

Three backends, selected by the ``LLM_PROVIDER`` env var:

- ``session`` (default): **orchestrator-in-the-loop** — the reasoning is done by
  the CURRENT Claude Code session (the orchestrator running these skills), so **no
  API key and no network/cost**. LLM-powered skills run in two steps: a ``prepare``
  step (deterministic Python) selects the rows that still need work and prints the
  exact prompt(s); the orchestrator reads them, produces the answers itself, and a
  ``save`` step (deterministic Python) writes the answers back to the store. The
  store rows ARE the work queue (e.g. jobs at ``matched`` without a ``jd_brief``),
  so the flow is naturally resumable. ``complete()`` is intentionally NOT callable
  here — there is no separate model to call; the orchestrator is the model.

- ``api``: calls the **Anthropic** Messages API with ``ANTHROPIC_API_KEY``.
- ``grok``: calls the **xAI (Grok)** chat-completions API with ``XAI_API_KEY``
  (OpenAI-compatible endpoint; uses stdlib ``urllib`` — no extra dependency).

For both API modes a skill's one-shot ``run`` step loops over the pending rows
calling :func:`complete`. The deterministic prepare/save logic is identical across
all three modes; only WHO answers the prompt differs. This module centralizes the
choice so every LLM skill behaves the same.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from execution.keypool import KeyPool, key_hint

# Rate-limit resilience for OpenAI-compatible hosts (Groq's free tier is a hard
# tokens-per-minute wall). On HTTP 429 we wait the server-suggested delay and retry.
MAX_RETRIES_429 = 5
MAX_BACKOFF_SECS = 30.0

# Proactive pacing: sleep BEFORE sending, so a sequential batch run stays under a
# host's per-minute request cap instead of firing 429s and backing off after the
# fact. Researched 2026-07-11 (PLAN.md §9):
#   Groq (api.groq.com)              — 30 RPM is Groq's own documented free-tier
#                                       cap for llama-3.3-70b-versatile, enforced
#                                       per-ORGANIZATION (not per-key — rotating
#                                       keys on the same account doesn't help).
#                                       console.groq.com/docs/rate-limits
#   NVIDIA NIM (integrate.api.nvidia.com) — NVIDIA publishes NO official rate
#                                       limit for the free API-catalog tier;
#                                       ~40 RPM is a community-reported (forum),
#                                       not contractual, figure. Paced at the
#                                       same conservative 30 RPM budget since
#                                       there's no documented Retry-After either.
#   DeepSeek (api.deepseek.com)        — DeepSeek's own docs say they do NOT
#                                       enforce an RPM/TPM cap; 429 instead comes
#                                       from a dynamic per-account concurrency
#                                       limit. A light interval still avoids
#                                       pointless request churn, but the real
#                                       DeepSeek failure mode is 402 Insufficient
#                                       Balance (a billing issue, not a rate limit
#                                       — no amount of pacing fixes that).
_HOST_MIN_INTERVAL_SECS = {
    "api.groq.com": 2.0,               # 60s / 30 RPM
    "integrate.api.nvidia.com": 2.0,   # no official cap published; same conservative budget
    "api.deepseek.com": 0.5,           # no documented RPM cap — light pacing only
}
_DEFAULT_MIN_INTERVAL = 1.0  # unrecognized OpenAI-compatible host — stay conservative

# Unsynchronized by design: every current caller (understand.py, tailor.py,
# respond.py, llm_rank.py) runs its LLM loop single-threaded within one
# subprocess, and the server's `_pipeline_lock` ensures only one such
# subprocess runs at a time. If a future caller ever parallelizes LLM calls
# with a thread pool, this dict needs a lock.
_last_request_at: dict[str, float] = {}


def _pace(base_url: str) -> None:
    """Block just long enough to keep sequential requests to `base_url`'s host
    under its documented (or best-known) per-minute cap. Per-process, in-memory —
    each `main.py prep`/`rank` run paces its own sequential loop; it doesn't need
    to survive across runs since a fresh process starts its own request cadence."""
    host = urllib.parse.urlparse(base_url).netloc
    interval = _HOST_MIN_INTERVAL_SECS.get(host, _DEFAULT_MIN_INTERVAL)
    last = _last_request_at.get(host)
    now = time.monotonic()
    if last is not None:
        wait = interval - (now - last)
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
    _last_request_at[host] = now


def _retry_after_secs(exc: "urllib.error.HTTPError", body: str) -> float:
    """Seconds to wait before retrying a 429: prefer the Retry-After header, else parse
    'try again in 12.885s' from the error body, else a 5s default. Capped for safety."""
    hdr = exc.headers.get("retry-after") if exc.headers else None
    if hdr:
        try:
            return min(float(hdr) + 0.5, MAX_BACKOFF_SECS)
        except ValueError:
            pass
    m = re.search(r"try again in ([\d.]+)\s*s", body)
    if m:
        try:
            return min(float(m.group(1)) + 0.5, MAX_BACKOFF_SECS)
        except ValueError:
            pass
    return 5.0

# Per-provider default model (overridable with LLM_MODEL).
DEFAULT_MODELS = {
    "api": "claude-sonnet-4-6",
    "grok": "grok-4",
}
DEFAULT_MODEL = DEFAULT_MODELS["api"]  # back-compat alias

# Providers that call out to a real model (everything else => session mode, the
# safe/free default — an unknown/typo'd provider falls back to session, never a
# surprise API charge).
API_PROVIDERS = frozenset({"api", "grok"})


class SessionModeError(RuntimeError):
    """Raised when api-only code (``complete``) runs under LLM_PROVIDER=session."""


def is_infra_error(exc: Exception) -> bool:
    """True for failures no retry can fix by trying a different model or a
    different job — rate limits, auth, dead connections. False (default) for
    failures plausibly specific to THIS one call (bad JSON, a single 500,
    content filter) — those are worth continuing past rather than aborting a
    whole batch. Shared by the backup-model retry below and by skill run
    loops (jd-understander, resume-tailor, humanise-responder) deciding
    abort-vs-skip-and-continue on a job's API error. See PLAN §9 2026-08-23
    for the job-850 5xx repro this classification was written to fix.

    Status codes are matched with \\b word boundaries, not bare substrings
    (code-reviewer MINOR, 2026-08-23): a 500's error body can legitimately
    contain a trace/request id like "...14019..." which contains "401" as a
    substring — that must NOT misclassify a one-off 500 as an infra error and
    abort a whole batch over it."""
    msg = str(exc).lower()
    return bool(re.search(r"\b(429|401|403)\b", msg)) or (
        "rate-limit" in msg or "unauthorized" in msg or "request failed" in msg)


def provider() -> str:
    """Current LLM backend: ``"session"`` (default), ``"api"``, or ``"grok"``."""
    return (os.environ.get("LLM_PROVIDER") or "session").strip().lower()


def is_session_mode() -> bool:
    """True unless an API-backed provider is selected (``api``/``grok``)."""
    return provider() not in API_PROVIDERS


def model() -> str:
    """Model id for API mode (``LLM_MODEL`` override, else the provider default)."""
    return os.environ.get("LLM_MODEL") or DEFAULT_MODELS.get(provider(), DEFAULT_MODEL)


def complete(prompt: str, *, system: str = "", max_tokens: int = 2048,
             model_id: str | None = None, temperature: float | None = None) -> str:
    """Return the model's text answer to ``prompt`` (API modes only).

    Dispatches on ``LLM_PROVIDER``: ``api`` → Anthropic, ``grok`` → xAI. In
    ``session`` mode this raises :class:`SessionModeError`: there is no separate
    model to call — the orchestrator answers the prepared prompts directly and the
    skill's ``save`` step stores them. Switch ``LLM_PROVIDER=api`` (with
    ``ANTHROPIC_API_KEY``) or ``LLM_PROVIDER=grok`` (with ``XAI_API_KEY``) to use
    this path.
    """
    p = provider()
    if p == "api":
        return _complete_anthropic(prompt, system=system, max_tokens=max_tokens,
                                   model_id=model_id, temperature=temperature)
    if p == "grok":
        backup = os.environ.get("LLM_BACKUP_MODEL", "").strip()
        try:
            return _complete_grok(prompt, system=system, max_tokens=max_tokens,
                                  model_id=model_id, temperature=temperature)
        except Exception as exc:
            effective = model_id or model()
            # Only retry with the backup for model-specific failures (model not found,
            # content filter, overload). Skip the retry for infrastructure errors that
            # switching models cannot fix — rate limits (429), auth (401/403), and network.
            if backup and backup != effective and not is_infra_error(exc):
                print(f"  ⚠ model {effective!r} failed ({type(exc).__name__}); "
                      f"retrying with backup {backup!r}…", file=sys.stderr)
                return _complete_grok(prompt, system=system, max_tokens=max_tokens,
                                      model_id=backup, temperature=temperature)
            raise
    raise SessionModeError(
        f"complete() is unavailable under LLM_PROVIDER={p!r} — the orchestrator "
        "answers the skill's prepared prompts and the `save` step writes them. "
        "Set LLM_PROVIDER=api (Anthropic) or LLM_PROVIDER=grok (xAI/NVIDIA/Groq) to call a model."
    )


def _complete_anthropic(prompt: str, *, system: str, max_tokens: int,
                        model_id: str | None, temperature: float | None = None) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("LLM_PROVIDER=api but ANTHROPIC_API_KEY is not set in .env")
    try:
        import anthropic  # lazy: only needed in api mode
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "LLM_PROVIDER=api needs the `anthropic` package — `pip install anthropic`"
        ) from exc

    client = _anthropic(anthropic, api_key)
    kwargs: dict = {
        "model": model_id or model(),
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    if temperature is not None:
        kwargs["temperature"] = temperature
    try:
        resp = client.messages.create(**kwargs)
    except anthropic.APIError as exc:  # uniform RuntimeError across providers
        raise RuntimeError(f"Anthropic API error: {exc}") from exc
    return "".join(
        block.text for block in resp.content
        if getattr(block, "type", None) == "text"
    )


_anthropic_client = None  # cached client (reuses the httpx connection pool)
_anthropic_key: str | None = None


def _anthropic(anthropic_mod, api_key: str):
    """Return a cached Anthropic client, rebuilt only if the key changes."""
    global _anthropic_client, _anthropic_key
    if _anthropic_client is None or _anthropic_key != api_key:
        _anthropic_client = anthropic_mod.Anthropic(api_key=api_key)
        _anthropic_key = api_key
    return _anthropic_client


def _grok_classify(exc: BaseException) -> str | None:
    """Map a Grok/OpenAI-compatible HTTP error to a key-health status (for the pool)."""
    code = getattr(exc, "code", None)
    if code == 429:
        return "throttled"          # per-minute TPM — transient, a different key is fresh
    if code in (401, 403):
        return "invalid"            # bad/blocked key
    return None


def _grok_pool() -> KeyPool:
    """Grok/Groq key pool: keys from XAI_API_KEY / GROK_API_KEY (one-or-many) + _1/_2/….
    A 429'd key is `throttled` (short cooldown, since TPM resets per minute), not exhausted."""
    root = Path(__file__).resolve().parent.parent
    state = os.environ.get("LLM_KEYS_STATE") or (root / "data" / "grok_keys.json")
    return KeyPool(
        name="grok",
        env_primaries=["XAI_API_KEY", "GROK_API_KEY"],
        state_path=state,
        classify_error=_grok_classify,
        degraded="throttled",
        recovery="cooldown",
        cooldown_secs=90,
        statuses=("healthy", "unknown", "throttled", "invalid"),
    )


def _grok_send(url: str, body: bytes, key: str, *, timeout: int = 120) -> str:
    """One chat-completions POST with a specific key; returns raw text or raises HTTPError."""
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Some OpenAI-compatible hosts (Groq) sit behind Cloudflare, which 403s the
            # default Python-urllib UA. Send a normal client UA.
            "User-Agent": "job-search-pipeline/1.0 (+https://github.com/varakumar01)",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _complete_grok(prompt: str, *, system: str, max_tokens: int,
                   model_id: str | None, temperature: float | None = None) -> str:
    """Call the xAI/Grok (or Groq) OpenAI-compatible chat-completions endpoint.

    Multi-key aware: rotates over all configured keys (``XAI_API_KEY``/``GROK_API_KEY``
    one-or-many, plus ``_1``/``_2``/…) best-health-first. On a 429 it ROTATES to the next
    key (a fresh key has its own TPM bucket — better than waiting); only when every key is
    throttled does it wait-and-retry. Endpoint overridable via ``XAI_BASE_URL``. Stdlib only.
    """
    pool = _grok_pool()
    candidates = pool.ordered_candidates()
    if not candidates:
        raise RuntimeError("LLM_PROVIDER=grok but no XAI_API_KEY / GROK_API_KEY set in .env")
    base = (os.environ.get("XAI_BASE_URL") or "https://api.x.ai/v1").rstrip("/")
    url = f"{base}/chat/completions"

    # If LLM_SYSTEM_PREFIX is set (e.g. "detailed thinking off"), prepend it to the
    # system prompt. NOTE (research 2026-08-23, see PLAN §9): this string toggle is
    # NVIDIA's documented mechanism only for `llama-3.3-nemotron-super-49b-v1` /
    # `llama-3.1-nemotron-ultra-*` — it is NOT documented for `nemotron-3-super-*`
    # (the model this codebase actually calls) and was found to have no reliable
    # effect on it. Kept as a harmless legacy nudge (no-op for models that don't
    # recognize it); the REAL control for nemotron-3-super is the
    # `chat_template_kwargs.enable_thinking` request field below.
    prefix = (os.environ.get("LLM_SYSTEM_PREFIX") or "").strip()
    effective_system = (f"{prefix}\n{system}".strip() if prefix else system)

    messages: list[dict] = []
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    messages.append({"role": "user", "content": prompt})

    # Generic escape hatch: any provider-specific top-level request field a skill
    # never needs to know about (e.g. NVIDIA NIM's `chat_template_kwargs`) gets
    # merged straight into the JSON body via LLM_EXTRA_BODY (a JSON object string,
    # set per-provider by main.py's `_llm_env()`). Malformed input is dropped with
    # a warning rather than failing the whole request — this is a tuning knob, not
    # a hard dependency.
    # Reserved: this function computes these itself from its own arguments — a tuning
    # knob must not be able to silently override the model/budget/prompt it sits
    # alongside (code-reviewer MAJOR, 2026-08-23).
    _RESERVED_BODY_KEYS = {"model", "max_tokens", "messages", "temperature"}
    # Only apply it to the PRIMARY model (model_id is None → caller wants the
    # configured default). `complete()`'s backup-model retry above is the one
    # caller that ever passes an explicit `model_id` override, and the backup
    # (e.g. mistral-nemotron) is a different model that may not accept fields
    # tuned for the primary (e.g. nemotron-3-super's `chat_template_kwargs`) —
    # sending them there produced a bare "xAI API error 500" (PLAN §9
    # 2026-08-23, job 850 repro). A tuning knob for model A must not leak onto
    # a request explicitly addressed to model B.
    extra_body: dict = {}
    extra_body_raw = (os.environ.get("LLM_EXTRA_BODY") or "").strip() if model_id is None else ""
    if extra_body_raw:
        try:
            parsed = json.loads(extra_body_raw)
            if isinstance(parsed, dict):
                clashes = _RESERVED_BODY_KEYS & parsed.keys()
                if clashes:
                    print(f"  ⚠ LLM_EXTRA_BODY may not set {sorted(clashes)} — dropping those "
                          f"key(s), keeping the rest", file=sys.stderr)
                    parsed = {k: v for k, v in parsed.items() if k not in _RESERVED_BODY_KEYS}
                extra_body = parsed
            else:
                print(f"  ⚠ LLM_EXTRA_BODY is not a JSON object, ignoring: {extra_body_raw!r}",
                      file=sys.stderr)
        except json.JSONDecodeError:
            print(f"  ⚠ LLM_EXTRA_BODY is not valid JSON, ignoring: {extra_body_raw!r}",
                  file=sys.stderr)

    # Safety net for reasoning models that still eat the token budget on a hidden
    # <think> block despite the toggles above: retry ONCE with a much larger budget
    # if the response comes back truncated (`finish_reason == "length"`), before the
    # caller's JSON parse ever sees it. Lives here (not a per-skill max_tokens bump)
    # because it's a completions-API-level signal, not JSON-specific — every
    # `llm.complete()` caller benefits. Full investigation/repro: PLAN §9 2026-08-23.
    attempt_tokens = max_tokens
    content = ""
    for attempt in range(2):
        payload: dict = {"model": model_id or model(), "max_tokens": attempt_tokens,
                         "messages": messages, **extra_body}
        if temperature is not None:
            payload["temperature"] = temperature
        body = json.dumps(payload).encode("utf-8")

        raw: str | None = None
        last_err: Exception | None = None
        throttled: list[tuple[str, float]] = []

        # Pass 1: try each key ONCE, rotating past a throttled/invalid key immediately.
        for key in candidates:
            try:
                _pace(base)
                raw = _grok_send(url, body, key)
                pool.mark(key, "healthy")
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                safe = detail.replace(key, key_hint(key))  # never surface the raw key
                if exc.code == 429:
                    pool.mark(key, "throttled", error=safe[:200])
                    throttled.append((key, _retry_after_secs(exc, detail)))
                    if len(candidates) > 1:
                        print(f"  ⏳ key {key_hint(key)} rate-limited (429) — rotating to next key…",
                              file=sys.stderr)
                    continue
                if exc.code in (401, 403):
                    pool.mark(key, "invalid", error=safe[:200])
                    last_err = RuntimeError(f"xAI API error {exc.code}: {safe}")
                    continue
                raise RuntimeError(f"xAI API error {exc.code}: {safe}") from exc
            except urllib.error.URLError as exc:  # pragma: no cover - network-dependent
                raise RuntimeError(f"xAI API request failed: {exc.reason}") from exc

        # Pass 2: every key was throttled → wait the shortest delay and retry that key.
        if raw is None and throttled:
            key, wait = min(throttled, key=lambda kw: kw[1])
            for retry in range(MAX_RETRIES_429):
                print(f"  ⏳ all grok keys throttled; waiting {wait:.0f}s then retrying "
                      f"{key_hint(key)} ({retry + 1}/{MAX_RETRIES_429})…", file=sys.stderr)
                time.sleep(min(wait, MAX_BACKOFF_SECS))
                try:
                    # The Retry-After sleep above (min 5s) already exceeds every
                    # host's pacing interval (max 2s), so this never adds extra
                    # delay here — it only updates _last_request_at's bookkeeping.
                    _pace(base)
                    raw = _grok_send(url, body, key)
                    pool.mark(key, "healthy")
                    break
                except urllib.error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", "replace")
                    if exc.code == 429:
                        pool.mark(key, "throttled", error=detail.replace(key, key_hint(key))[:200])
                        wait = _retry_after_secs(exc, detail)
                        continue
                    raise RuntimeError(f"xAI API error {exc.code}: "
                                       f"{detail.replace(key, key_hint(key))}") from exc
                except urllib.error.URLError as exc:  # pragma: no cover
                    raise RuntimeError(f"xAI API request failed: {exc.reason}") from exc

        if raw is None:
            # If any key was throttled, that's the operational reason (not a stale 401 from an
            # invalid key) — surface it accurately.
            if throttled:
                raise RuntimeError("all grok keys are rate-limited (429) — retries exhausted; "
                                   "wait ~1 min or add another XAI_API_KEY")
            raise last_err or RuntimeError("all grok keys are invalid/unauthorized")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:  # 200 OK but not JSON (proxy/CDN error page)
            raise RuntimeError(f"xAI returned non-JSON response: {raw[:500]}") from exc
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected xAI response shape: {data!r}") from exc

        if choice.get("finish_reason") == "length" and attempt == 0:
            attempt_tokens = min(attempt_tokens * 4, 8192)
            print(f"  ⏳ response cut off at max_tokens={max_tokens} "
                  f"(finish_reason=length, likely reasoning-mode overhead) — "
                  f"retrying once with max_tokens={attempt_tokens}…", file=sys.stderr)
            continue
        break

    return content
