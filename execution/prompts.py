"""Prompt externalization — lets a user edit LLM system prompts without touching
skill code. See PLAN.md §8 Phase 10 frontend work.

Each skill still defines its prompt as a SYSTEM_PROMPT constant (the source of
truth / fallback). `load_prompt()` overrides it from `prompts/<name>.txt` if
that file exists and is non-empty — skill behavior is unchanged until a user
actually edits a file. `get_or_seed()` is for the frontend API: it writes the
skill's default to disk the first time a prompt is opened for editing, so
there's always something to show/edit.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"

# Every editable prompt, by name -> the skill script that owns its default.
PROMPT_NAMES = ("jd-understander", "humanise-responder", "profile-matcher")


def _path(name: str) -> Path:
    return PROMPTS_DIR / f"{name}.txt"


def load_prompt(name: str, default: str) -> str:
    """Return the prompt named `name` — from `prompts/<name>.txt` if present
    and non-empty, else `default` (the skill's own inline constant). Read-only,
    no side effects — safe to call on every skill run."""
    path = _path(name)
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return default


def get_or_seed(name: str, default: str) -> str:
    """Like `load_prompt`, but writes `default` to disk first if the file
    doesn't exist yet. Used by the frontend API so opening the prompt editor
    always has real content to show, without changing skill behavior on a
    plain skill run (which only ever calls `load_prompt`)."""
    if name not in PROMPT_NAMES:
        raise ValueError(f"unknown prompt {name!r}; valid: {PROMPT_NAMES}")
    path = _path(name)
    if not path.exists():
        PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(default, encoding="utf-8")
    return path.read_text(encoding="utf-8")


def save_prompt(name: str, text: str) -> None:
    if name not in PROMPT_NAMES:
        raise ValueError(f"unknown prompt {name!r}; valid: {PROMPT_NAMES}")
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    _path(name).write_text(text, encoding="utf-8")
