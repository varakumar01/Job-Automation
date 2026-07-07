"""Multi-key Apify token store with health tracking + auto-rotation.

Thin Apify-specific wrapper over the generic :class:`execution.keypool.KeyPool` (the
rotation/health/secret-scrubbing logic lives there and is shared with the Grok key pool).
This module keeps the same public API the Apify path already imports
(``best_key``/``ordered_candidates``/``classify_error``/``mark``/``key_hint``/
``status_table``/``reset``) so ``_apify.py`` and ``scrape.py`` are untouched.

**Configuring keys** (any mix, `.env`): ``APIFY_TOKEN`` may hold ONE key or SEVERAL
(comma/space/semicolon/newline separated); ``APIFY_TOKEN_1``, ``APIFY_TOKEN_2``, … one each.

**Health** (persisted in ``data/apify_keys.json`` — masked hint + hash only, never the
secret): ``healthy`` (last run ok) · ``unknown`` (unused) · ``exhausted`` (monthly credit
used up; auto-resets at month rollover) · ``invalid`` (token rejected). Rotation order:
``healthy → unknown → exhausted → invalid``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the repo root is importable so `execution.keypool` resolves even when this
# module is loaded via the plugins-dir-on-sys.path path.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from execution.keypool import KeyPool, key_hint, key_id  # noqa: E402  (re-exported)

_STATE_DEFAULT = _ROOT / "data" / "apify_keys.json"


def classify_error(exc: BaseException) -> str | None:
    """Map an Apify error to a key-health status, or None if it's not key-related.

    'invalid' → the token is bad (auth). 'exhausted' → usage/credit/quota limit. None → a
    non-key problem (bad input, timeout, network, or a transient 429) — the same error
    would hit every key, so rotation must NOT swallow it.
    """
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    msg = str(getattr(exc, "message", "") or exc).lower()

    if code == 429 or "rate limit" in msg or "too many requests" in msg:
        return None  # transient throttle, not a dead key

    invalid_signals = ("invalid token", "token is invalid", "token is not valid",
                       "bad token", "unauthorized", "not authorized",
                       "authentication failed", "invalid api", "user was not found",
                       "permission", "access denied", "not allowed")
    exhausted_signals = ("usage limit", "monthly usage", "credit limit", "out of credit",
                        "insufficient credit", "no credit", "quota", "plan limit",
                        "payment required", "billing", "monthly limit")

    if any(s in msg for s in invalid_signals):
        return "invalid"
    if any(s in msg for s in exhausted_signals):
        return "exhausted"
    if code == 401:
        return "invalid"
    if code in (402, 403):  # Apify returns 403 when the plan's monthly credit is used up
        return "exhausted"
    return None


def _pool() -> KeyPool:
    """Build the Apify pool (reads APIFY_KEYS_STATE each call so the override stays dynamic)."""
    state = os.environ.get("APIFY_KEYS_STATE") or _STATE_DEFAULT
    return KeyPool(
        name="apify",
        env_primaries=["APIFY_TOKEN"],
        state_path=state,
        classify_error=classify_error,
        degraded="exhausted",
        recovery="monthly",
        statuses=("healthy", "unknown", "exhausted", "invalid"),
    )


# ── public API (delegates to the shared KeyPool) ───────────────────────────
def collect_keys() -> list[str]:
    return _pool().collect_keys()


def load_state() -> dict:
    return _pool().load_state()


def mark(key: str, status: str, *, error: str | None = None) -> None:
    _pool().mark(key, status, error=error)


def status_of(state: dict, key: str) -> str:
    return _pool().status_of(state, key)


def ordered_candidates() -> list[str]:
    return _pool().ordered_candidates()


def best_key() -> str | None:
    return _pool().best_key()


def status_table() -> list[dict]:
    return _pool().status_table()


def reset(which: str = "all") -> int:
    return _pool().reset(which)
