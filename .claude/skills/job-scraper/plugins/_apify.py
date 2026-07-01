"""Shared Apify helpers for the Apify-backed job-source plugins.

Leading underscore → the registry skips this module during plugin discovery
(see registry.py ``_SKIP`` / underscore rule), so it is a pure helper, never a
plugin. Both ``linkedin.py``, ``naukri.py`` and ``indeed.py`` import from here.

Cost control: every adapter passes its ``limit`` into the actor's own count
field (``count`` / ``maxRows`` / ``maxJobs``) so the actor only *produces* what
we ask for — pay-per-event actors charge per produced result, not per item we
read back. See PLAN.md §6 (conservative limits).
"""

from __future__ import annotations

import os
import re
from datetime import timedelta
from decimal import Decimal
from typing import Any

from apify_client import ApifyClient

# Hard per-run dollar ceiling (safety net on the FREE plan). Override with
# APIFY_MAX_CHARGE_USD. Bounds spend even if an actor ignores the count field.
_DEFAULT_MAX_CHARGE_USD = 0.50


def get_token() -> str | None:
    """Best Apify token to try right now, or None if none are configured.

    Backed by the multi-key store (:mod:`_apify_keys`): a single ``APIFY_TOKEN``
    still works, but several keys (comma-separated or ``APIFY_TOKEN_1/2/…``) are
    rotated by health. Used by each plugin's ``is_available()``.
    """
    from _apify_keys import best_key
    return best_key()


def actor_id(env_var: str, default: str) -> str:
    """Allow overriding an actor id via env (e.g. APIFY_ACTOR_LINKEDIN)."""
    return os.environ.get(env_var) or default


def _max_charge() -> Decimal:
    try:
        return Decimal(os.environ.get("APIFY_MAX_CHARGE_USD", str(_DEFAULT_MAX_CHARGE_USD)))
    except Exception:
        return Decimal(str(_DEFAULT_MAX_CHARGE_USD))


def _run_actor_once(
    client: "ApifyClient",
    actor: str,
    run_input: dict[str, Any],
    *,
    timeout_secs: int,
    memory_mbytes: int | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    """One actor run on one client; returns dataset items, raises on failure."""
    run = client.actor(actor).call(
        run_input=run_input,
        run_timeout=timedelta(seconds=timeout_secs),
        memory_mbytes=memory_mbytes,
        max_total_charge_usd=_max_charge(),
        # logger=None disables apify-client's live log-streaming thread. That thread can
        # raise a non-fatal impit.TimeoutException (Request timeout) mid-scrape and print
        # a scary traceback even though the run succeeds — we don't need the streamed logs.
        logger=None,
    )
    if run is None:
        raise RuntimeError(f"actor {actor!r} did not return a run object")

    def _attr(obj: Any, *names: str) -> Any:
        # apify-client 3.x returns a pydantic Run model (snake_case); older/raw
        # paths return a dict (camelCase). Support both.
        for n in names:
            if isinstance(obj, dict) and n in obj:
                return obj[n]
            if hasattr(obj, n):
                return getattr(obj, n)
        return None

    status = _attr(run, "status")
    status_str = str(getattr(status, "value", status) or "")
    if not status_str:
        raise RuntimeError(f"actor {actor!r} run returned no status (run id {_attr(run, 'id')})")
    if status_str != "SUCCEEDED":
        msg = _attr(run, "status_message", "statusMessage")
        raise RuntimeError(
            f"actor {actor!r} run ended with status {status_str!r}"
            f"{f' — {msg}' if msg else ''} (run id {_attr(run, 'id')})"
        )
    dataset_id = _attr(run, "default_dataset_id", "defaultDatasetId")
    if not dataset_id:
        raise RuntimeError(f"actor {actor!r} run has no default dataset id")

    items: list[dict[str, Any]] = []
    for item in client.dataset(dataset_id).iterate_items(limit=limit):
        items.append(item)
        if limit is not None and len(items) >= limit:
            break  # defensive: also stop client-side in case limit= isn't honored
    return items


def run_actor(
    actor: str,
    run_input: dict[str, Any],
    *,
    token: str | None = None,
    timeout_secs: int = 300,
    memory_mbytes: int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Run an Apify actor to completion and return its dataset items, rotating keys.

    With ``token=None`` (the normal path) the configured Apify keys are tried in
    health order (see :mod:`_apify_keys`): a key that returns an auth error is
    marked ``invalid`` and a usage/credit-limit error marks it ``exhausted``, and
    the next key is tried — so scraping keeps going as long as one key has credit.
    A non-key error (bad actor input, timeout) is NOT swallowed: it would hit every
    key, so it propagates immediately. Pass an explicit ``token`` to force a single
    key (used by tests).

    Cost is bounded two ways: the count field inside ``run_input`` (each adapter
    passes ``limit`` there, floored to the actor's minimum) and a hard
    ``max_total_charge_usd`` ceiling (APIFY_MAX_CHARGE_USD, default $0.50) — applied
    per key, so rotation never multiplies the per-run ceiling within one attempt. We
    do NOT pass ``.call(max_items=…)``; ``limit`` is applied when reading instead.
    """
    import _apify_keys as keys

    candidates = [token] if token is not None else keys.ordered_candidates()
    if not candidates:
        raise RuntimeError("no Apify token configured (set APIFY_TOKEN in .env)")

    errors: list[str] = []
    for key in candidates:
        client = ApifyClient(key)
        try:
            items = _run_actor_once(
                client, actor, run_input,
                timeout_secs=timeout_secs, memory_mbytes=memory_mbytes, limit=limit,
            )
        except Exception as exc:  # noqa: BLE001 — classify, then rotate or re-raise
            health = keys.classify_error(exc)
            if health is None:
                raise  # not a key problem — another key won't fix it
            keys.mark(key, health, error=str(exc))
            errors.append(f"{keys.key_hint(key)}→{health}")
            continue
        keys.mark(key, "healthy")
        return items

    raise RuntimeError(
        f"all {len(candidates)} Apify key(s) failed for actor {actor!r}: {', '.join(errors)}. "
        f"Add credit, add another APIFY_TOKEN, or check key health (`scrape.py --keys`)."
    )


def first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present, non-empty value among ``keys`` (dotted paths ok)."""
    for key in keys:
        val: Any = d
        ok = True
        for part in key.split("."):
            if isinstance(val, dict) and part in val:
                val = val[part]
            else:
                ok = False
                break
        if ok and val not in (None, "", [], {}):
            return val
    return default


def as_text(val: Any) -> str | None:
    """Coerce a portal field into a plain string the store can persist.

    Portals sometimes return structured values (a location dict, a list of job
    types). Strings/None pass through; dicts prefer a formatted/display field,
    else join their place parts; lists are comma-joined.
    """
    if val is None or isinstance(val, str):
        return val or None
    if isinstance(val, dict):
        for k in ("formattedAddress", "formatted_address", "displayName", "name", "label", "text"):
            if val.get(k):
                return str(val[k])
        parts = [str(val[k]) for k in ("city", "region", "state", "country") if val.get(k)]
        return ", ".join(parts) or None
    if isinstance(val, (list, tuple)):
        seen: list[str] = []
        for x in val:
            t = as_text(x) if isinstance(x, (dict, list, tuple)) else (str(x) if x not in (None, "") else None)
            if t and t not in seen:
                seen.append(t)
        return ", ".join(seen) or None
    return str(val)


def first_text(d: dict[str, Any], *keys: str, default: Any = None) -> str | None:
    """Like ``first`` but returns the first candidate that yields non-empty *text*.

    ``first`` stops at the first present key; if that value is a structure with no
    extractable text (e.g. a ``companyDetail`` dict lacking ``name``), the later
    string candidate would be lost. ``first_text`` applies :func:`as_text` to each
    candidate and returns the first that flattens to a real string.
    """
    for key in keys:
        text = as_text(first(d, key))
        if text:
            return text
    return as_text(default)


def derive_ext_id(item: dict[str, Any], url: str | None, *id_keys: str) -> str | None:
    """Best-effort stable id: an explicit id field, else a numeric id in the URL."""
    val = first(item, *id_keys)
    if val not in (None, ""):
        return str(val)
    if url and isinstance(url, str):
        m = re.search(r"(\d{6,})", url)
        if m:
            return m.group(1)
    return None
