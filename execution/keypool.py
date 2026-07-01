"""Generic multi-key pool with health tracking + auto-rotation.

Extracted from the Apify token rotation so the SAME logic serves any API that has several
interchangeable keys (Apify tokens, Groq/xAI keys, …). Each pool:
  - parses its keys from env (a primary var holding one-or-many + numbered `_1/_2/…` vars),
  - tracks per-key health in a JSON state file — only a masked hint + sha256 id, NEVER the
    secret,
  - orders candidates best-health-first (`healthy → unknown → degraded → invalid`),
  - auto-recovers a `degraded` key either after a **cooldown** (per-minute throttle, e.g.
    Groq TPM) or at **month rollover** (monthly credit, e.g. Apify free plan).

Health states are the four in ``statuses`` (default: healthy/unknown/exhausted/invalid);
the "degraded" tier name + recovery policy are configurable per pool.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

_SPLIT = re.compile(r"[\s,;]+")


def key_id(key: str) -> str:
    """Stable, non-reversible id for a key (so state never stores the secret)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def key_hint(key: str) -> str:
    """Masked display hint — enough to recognize the key, not to use it."""
    tail = key[-5:] if len(key) >= 5 else key
    return f"…{tail}"


class KeyPool:
    """A health-tracked, auto-rotating pool of interchangeable API keys."""

    def __init__(
        self,
        *,
        name: str,
        env_primaries: Sequence[str],
        state_path: str | os.PathLike[str],
        classify_error: Callable[[BaseException], str | None],
        degraded: str = "exhausted",
        invalid: str = "invalid",
        recovery: str = "monthly",          # "monthly" | "cooldown"
        cooldown_secs: int = 90,
        statuses: Sequence[str] = ("healthy", "unknown", "exhausted", "invalid"),
    ) -> None:
        self.name = name
        self.env_primaries = list(env_primaries)
        self.state_path = Path(state_path)
        self.classify_error = classify_error
        self.degraded = degraded
        self.invalid = invalid
        self.recovery = recovery
        self.cooldown = timedelta(seconds=cooldown_secs)
        self.statuses = tuple(statuses)
        self.priority = {s: i for i, s in enumerate(self.statuses)}

    # ── keys from env ────────────────────────────────────────────────────────
    def collect_keys(self) -> list[str]:
        """All configured keys, in config order, de-duplicated."""
        keys: list[str] = []
        seen: set[str] = set()

        def add(raw: str | None) -> None:
            for part in _SPLIT.split(raw or ""):
                k = part.strip()
                if k and k not in seen:
                    seen.add(k)
                    keys.append(k)

        for base in self.env_primaries:
            add(os.environ.get(base))
        # Numbered variants: <PRIMARY>_1 / <PRIMARY>1 / … in numeric order.
        numbered: list[tuple[int, str]] = []
        for env_name, val in os.environ.items():
            if not val:
                continue
            for base in self.env_primaries:
                m = re.fullmatch(re.escape(base) + r"_?(\d+)", env_name)
                if m:
                    numbered.append((int(m.group(1)), val))
                    break
        for _, val in sorted(numbered):
            add(val)
        return keys

    # ── state file ───────────────────────────────────────────────────────────
    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _month(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def load_state(self) -> dict[str, Any]:
        """Load health state, auto-recovering stale `degraded` keys per the pool policy."""
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            state = {}
        keys = state.get("keys")
        if not isinstance(keys, dict):
            state["keys"] = {}
            return state

        changed = False
        now = datetime.now(timezone.utc)
        for rec in keys.values():
            if rec.get("status") != self.degraded:
                continue
            recover = False
            if self.recovery == "monthly":
                recover = rec.get("degraded_month") != self._month()
            else:  # cooldown
                ts = rec.get("degraded_at")
                if ts:
                    try:
                        recover = (now - datetime.fromisoformat(ts)) > self.cooldown
                    except ValueError:
                        recover = True
                else:
                    recover = True
            if recover:
                rec["status"] = "unknown"
                rec["last_note"] = "auto-recovered"
                changed = True
        if changed:
            self._write_state(state)
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def _record(self, state: dict[str, Any], key: str) -> dict[str, Any]:
        rec = state.setdefault("keys", {}).setdefault(key_id(key), {})
        rec["hint"] = key_hint(key)
        rec.setdefault("status", "unknown")
        return rec

    def mark(self, key: str, status: str, *, error: str | None = None) -> None:
        """Persist a key's health after an attempt."""
        if status not in self.statuses:
            raise ValueError(f"unknown status {status!r} for pool {self.name!r}")
        state = self.load_state()
        rec = self._record(state, key)
        rec["status"] = status
        rec["checked_at"] = self._now()
        if status == "healthy":
            rec["last_ok"] = self._now()
            rec.pop("last_error", None)
        if error:
            # Scrub the secret in case an error echoes the key — the full key must NEVER
            # reach the on-disk state (spec: hint + hash only).
            rec["last_error"] = error.replace(key, key_hint(key))[:300]
        if status == self.degraded:
            rec["degraded_month"] = self._month()
            rec["degraded_at"] = self._now()
        self._write_state(state)

    def status_of(self, state: dict[str, Any], key: str) -> str:
        return state.get("keys", {}).get(key_id(key), {}).get("status", "unknown")

    def ordered_candidates(self) -> list[str]:
        """Configured keys, best-health-first (config order breaks ties)."""
        state = self.load_state()
        keys = self.collect_keys()
        return sorted(keys, key=lambda k: (self.priority.get(self.status_of(state, k), 9),
                                           keys.index(k)))

    def best_key(self) -> str | None:
        cands = self.ordered_candidates()
        return cands[0] if cands else None

    def status_table(self) -> list[dict[str, str]]:
        state = self.load_state()
        rows: list[dict[str, str]] = []
        for i, key in enumerate(self.collect_keys(), 1):
            rec = state.get("keys", {}).get(key_id(key), {})
            rows.append({
                "n": str(i),
                "hint": key_hint(key),
                "status": rec.get("status", "unknown"),
                "checked_at": rec.get("checked_at", "—"),
                "last_error": rec.get("last_error", ""),
            })
        return rows

    def reset(self, which: str = "all") -> int:
        """Clear health flags. ``which`` = 'all' | a status | a key-hint tail. Returns count."""
        state = self.load_state()
        n = 0
        for rec in state.get("keys", {}).values():
            st = rec.get("status", "unknown")
            if which == "all" or which == st or rec.get("hint", "").endswith(which):
                if st != "unknown":
                    rec["status"] = "unknown"
                    rec["last_note"] = f"manual reset ({which})"
                    rec.pop("last_error", None)
                    n += 1
        if n:
            self._write_state(state)
        return n
