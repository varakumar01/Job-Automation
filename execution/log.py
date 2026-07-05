"""Shared verbosity helper for all pipeline scripts.

Level 0 (default) — unchanged from before; all today's summary prints show.
Level 1 (-v)      — extra context: scoring breakdowns, actor IDs, brief fields.
Level 2 (-vv)     — full debug: raw LLM prompts, model replies, per-item traces.

main.py threads the level into every subprocess via JOBSEARCH_VERBOSITY in the env.
Scripts running standalone also accept -v/-vv directly via add_verbose_arg() +
apply_verbosity(), which take the max of the CLI flag and the env var so that a
higher level inherited from the parent process cannot be silently downgraded.
"""

from __future__ import annotations

import os
import sys


def verbosity() -> int:
    """Current verbosity level (0, 1, or 2) from the JOBSEARCH_VERBOSITY env var."""
    try:
        return min(2, max(0, int(os.environ.get("JOBSEARCH_VERBOSITY") or 0)))
    except (ValueError, TypeError):
        return 0


def vprint(level: int, *args, file=None, **kw) -> None:
    """Print *args if verbosity() >= level.  Defaults to stderr to keep stdout clean."""
    if verbosity() >= level:
        print(*args, file=file if file is not None else sys.stderr, **kw)


def add_verbose_arg(parser) -> None:
    """Add -v / -vv to an argparse.ArgumentParser (for standalone script invocation)."""
    parser.add_argument(
        "-v", "--verbose",
        action="count", default=0,
        help="-v: verbose (scoring details, actor IDs, brief fields); "
             "-vv: debug (full LLM prompts & raw replies)",
    )


def apply_verbosity(args) -> None:
    """Sync the parsed -v count into JOBSEARCH_VERBOSITY, keeping the higher of the
    CLI flag and any level already set by the parent process (main.py --llm ...)."""
    cli_level = getattr(args, "verbose", 0) or 0
    level = max(cli_level, verbosity())
    os.environ["JOBSEARCH_VERBOSITY"] = str(level)
