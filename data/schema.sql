-- AI Job-Search store schema (SQLite). See PLAN.md §3.
-- The `jobs` table is the pipeline spine; each skill advances `status`.

CREATE TABLE IF NOT EXISTS jobs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source               TEXT    NOT NULL,            -- portal: linkedin/naukri/indeed/...
    ext_id               TEXT    NOT NULL,            -- portal's job id
    url                  TEXT,
    title                TEXT,
    company              TEXT,
    location             TEXT,
    posted_at            TEXT,                        -- ISO-8601 if known
    jd_text              TEXT,                        -- raw job description
    jd_brief             TEXT,                        -- jd-understander output (JSON or prose)
    match_score          REAL,                        -- profile-matcher score 0-100 (deterministic)
    llm_score            REAL,                        -- llm_rank score 0-100 (Grok/LLM rerank)
    llm_reason           TEXT,                        -- one-line LLM fit rationale
    role_profile         TEXT,                        -- best-fit résumé variant
    status               TEXT    NOT NULL DEFAULT 'scraped',
                                                      -- scraped|matched|tailored|ready|applied|skipped|failed|rejected
    tailored_resume_path TEXT,
    answers_json         TEXT,                        -- humanise-responder answers (JSON)
    screenshot_path      TEXT,                        -- apply-agent review-gate screenshot
    applied_at           TEXT,
    outcome              TEXT,                        -- applied|skipped|failed + detail
    notes                TEXT,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, ext_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs (source);

-- One row per scrape/apply session, for auditing rate + volume.
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,                         -- scrape|apply
    source     TEXT,
    query      TEXT,
    counts     TEXT,                                  -- JSON: {found, new, updated, applied, ...}
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at   TEXT
);
