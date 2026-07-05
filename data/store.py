"""SQLite-backed pipeline store — the spine of the job-search pipeline.

See PLAN.md §3. Every skill reads rows by ``status`` and advances them, so a run
resumes after interruption. Pure stdlib (sqlite3 + json), no dependencies.

CLI (for inspection / smoke tests)::

    python3 data/store.py init
    python3 data/store.py stats
    python3 data/store.py list --status scraped
    python3 data/store.py export        # -> data/jobs.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

# Repo root = parent of this file's directory (data/).
ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

VALID_STATUSES = {
    "scraped", "matched", "tailored", "ready",
    "applied", "skipped", "failed",
    "rejected",  # off-profile / non-relevant — parked so the LLM never ranks/preps it
}

# Columns a scraper plugin may set when inserting a job.
_UPSERT_FIELDS = (
    "source", "ext_id", "url", "title", "company",
    "location", "posted_at", "jd_text",
)

# Columns update_job() is allowed to write (guards against typos/injection).
_UPDATABLE_FIELDS = {
    "url", "title", "company", "location", "posted_at", "jd_text",
    "jd_brief", "match_score", "llm_score", "llm_reason", "role_profile", "status",
    "tailored_resume_path", "answers_json", "screenshot_path",
    "applied_at", "outcome", "notes",
}


def parse_ids(raw: str | None) -> list[int] | None:
    """Parse a job-id selector like '1,2 3' → [1, 2, 3]. None/empty → None (no filter).
    Non-numeric tokens are ignored. Shared by main.py + the skill CLIs (`--jobs`)."""
    if not raw:
        return None
    out: list[int] = []
    for tok in str(raw).replace(" ", ",").split(","):
        tok = tok.strip()
        if tok:
            try:
                out.append(int(tok))
            except ValueError:
                pass
    return list(dict.fromkeys(out)) or None  # de-dupe, preserve order


def db_path() -> Path:
    """Resolve the DB path from JOBS_DB_PATH (env) or the default data/jobs.db."""
    raw = os.environ.get("JOBS_DB_PATH", "data/jobs.db")
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / p)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Yield a connection with row access by name and FKs on; commits on success."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create tables/indexes from schema.sql (idempotent), then migrate columns."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect() as conn:
        conn.executescript(schema)
        # Lightweight migration: ADD COLUMN for any field in the schema that an
        # older DB predates (CREATE TABLE IF NOT EXISTS won't add columns).
        have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        for col, decl in (("screenshot_path", "TEXT"),
                          ("llm_score", "REAL"), ("llm_reason", "TEXT")):
            if col not in have:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")


def upsert_jobs(jobs: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Insert new jobs, update existing ones (matched on source+ext_id).

    Each dict should carry at least ``source`` and ``ext_id``. Returns counts
    {"found", "new", "updated"}. New rows start at status ``scraped``.
    """
    found = new = updated = 0
    with connect() as conn:
        for job in jobs:
            found += 1
            source = job.get("source")
            ext_id = job.get("ext_id")
            if not source or not ext_id:
                raise ValueError("each job needs non-empty 'source' and 'ext_id'")
            ext_id = str(ext_id)

            row = conn.execute(
                "SELECT id FROM jobs WHERE source = ? AND ext_id = ?",
                (source, ext_id),
            ).fetchone()

            if row is None:
                cols = [c for c in _UPSERT_FIELDS if c in job]
                if "source" not in cols:
                    cols.append("source")
                if "ext_id" not in cols:
                    cols.append("ext_id")
                values = [str(ext_id) if c == "ext_id" else job.get(c) for c in cols]
                placeholders = ", ".join("?" for _ in cols)
                conn.execute(
                    f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({placeholders})",
                    values,
                )
                new += 1
            else:
                # Refresh only the scrape-sourced fields; never clobber pipeline state.
                sets = [c for c in _UPSERT_FIELDS
                        if c in job and c not in ("source", "ext_id")]
                if sets:
                    assignments = ", ".join(f"{c} = ?" for c in sets)
                    params: list[Any] = [job.get(c) for c in sets]
                    params.append(row["id"])
                    conn.execute(
                        f"UPDATE jobs SET {assignments}, "
                        f"updated_at = datetime('now') WHERE id = ?",
                        params,
                    )
                updated += 1
    return {"found": found, "new": new, "updated": updated}


def get_jobs(status: str | None = None, limit: int | None = None,
             order: str = "id") -> list[dict[str, Any]]:
    """Return jobs (optionally filtered by status).

    order:
      "id"    — oldest first (insertion order, the safe default for writers/upsert callers).
      "score" — best first: COALESCE(llm_score, match_score) DESC, then newest id DESC.
                Use this for display and prep batches so higher-rated jobs surface first.
    """
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"unknown status {status!r}; valid: {sorted(VALID_STATUSES)}")
    if order not in ("id", "score"):
        raise ValueError(f"unknown order {order!r}; valid: 'id', 'score'")
    sql = "SELECT * FROM jobs"
    params: list[Any] = []
    if status is not None:
        sql += " WHERE status = ?"
        params.append(status)
    if order == "score":
        sql += " ORDER BY COALESCE(llm_score, match_score) DESC, id DESC"
    else:
        sql += " ORDER BY id ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_job(job_id: int) -> dict[str, Any] | None:
    """Return one job by id, or None if it doesn't exist."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row is not None else None


def update_job(job_id: int, **fields: Any) -> None:
    """Update whitelisted columns on one job; always bumps updated_at."""
    if not fields:
        return
    bad = set(fields) - _UPDATABLE_FIELDS
    if bad:
        raise ValueError(f"cannot update non-whitelisted field(s): {sorted(bad)}")
    if "status" in fields and fields["status"] not in VALID_STATUSES:
        raise ValueError(f"invalid status {fields['status']!r}")
    assignments = ", ".join(f"{c} = ?" for c in fields)
    params: list[Any] = list(fields.values())
    params.append(job_id)
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE jobs SET {assignments}, updated_at = datetime('now') WHERE id = ?",
            params,
        )
        if cur.rowcount == 0:
            raise KeyError(f"no job with id {job_id}")


def log_run(kind: str, source: str | None = None, query: str | None = None,
            counts: dict[str, Any] | None = None) -> int:
    """Record a scrape/apply session; returns the run id."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (kind, source, query, counts, ended_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (kind, source, query, json.dumps(counts or {})),
        )
        return int(cur.lastrowid)


def stats() -> dict[str, int]:
    """Count jobs per status."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def export_json(path: str | os.PathLike[str] | None = None) -> Path:
    """Dump all jobs to JSON for human inspection. Returns the written path."""
    out = Path(path) if path else (db_path().parent / "jobs.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(get_jobs(), indent=2, ensure_ascii=False), encoding="utf-8")
    return out


# ── CLI ──────────────────────────────────────────────────────────────────

def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Job-search store admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create tables")
    sub.add_parser("stats", help="counts per status")
    sub.add_parser("export", help="write data/jobs.json")
    p_list = sub.add_parser("list", help="list jobs")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    if args.cmd == "init":
        init_db()
        print(f"✓ initialized {db_path()}")
    elif args.cmd == "stats":
        print(json.dumps(stats(), indent=2))
    elif args.cmd == "export":
        print(f"✓ exported {export_json()}")
    elif args.cmd == "list":
        for j in get_jobs(status=args.status, limit=args.limit):
            print(f"[{j['id']:>4}] {j['status']:<8} {j['source']:<9} "
                  f"{(j['title'] or '')[:40]:<40} {j['company'] or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
