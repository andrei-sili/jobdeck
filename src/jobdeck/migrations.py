"""Database schema management.

Migrations are strictly additive: new tables and new columns only. The
legacy `bewerbungen` table keeps its exact shape so the historical data
(and any legacy tooling still reading it) continues to work unchanged.
"""

import sqlite3

SCHEMA_VERSION = 2

# Legacy table, exactly as the previous tracker created it.
BEWERBUNGEN_SQL = """
CREATE TABLE IF NOT EXISTS bewerbungen (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gesendet_am     TEXT,
    firma           TEXT,
    email           TEXT,
    ansprechpartner TEXT,
    strasse         TEXT,
    plz_ort         TEXT,
    kanal           TEXT,
    status          TEXT,
    notiz           TEXT,
    dokument        TEXT,
    created_at      TEXT
)
"""

NEW_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS search_profiles (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    keywords          TEXT NOT NULL,
    location          TEXT NOT NULL DEFAULT '',
    radius_km         INTEGER NOT NULL DEFAULT 0,
    sources           TEXT NOT NULL DEFAULT '["arbeitsagentur","jooble","arbeitnow"]',
    active            INTEGER NOT NULL DEFAULT 1,
    auto_send         INTEGER NOT NULL DEFAULT 0,
    poll_interval_min INTEGER NOT NULL DEFAULT 60,
    last_polled_at    TEXT,
    last_poll_error   TEXT,
    created_at        TEXT NOT NULL,
    hard_tags         TEXT NOT NULL DEFAULT '',
    soft_preferences  TEXT NOT NULL DEFAULT '',
    strictness        INTEGER NOT NULL DEFAULT 50
);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id    INTEGER REFERENCES search_profiles(id),
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    company       TEXT NOT NULL DEFAULT '',
    location      TEXT NOT NULL DEFAULT '',
    remote        INTEGER NOT NULL DEFAULT 0,
    url           TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    published_at  TEXT NOT NULL DEFAULT '',
    fetched_at    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'new',
    match_score   INTEGER,
    match_reason  TEXT NOT NULL DEFAULT '',
    duplicate_of  INTEGER REFERENCES bewerbungen(id),
    bewerbung_id  INTEGER REFERENCES bewerbungen(id),
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS drafts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           INTEGER NOT NULL REFERENCES jobs(id),
    status           TEXT NOT NULL DEFAULT 'generating',
    recipient        TEXT NOT NULL DEFAULT '',
    betreff          TEXT NOT NULL DEFAULT '',
    email_body       TEXT NOT NULL DEFAULT '',
    anschreiben_body TEXT NOT NULL DEFAULT '',
    pdf_path         TEXT NOT NULL DEFAULT '',
    llm_model        TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT '',
    gmail_message_id TEXT NOT NULL DEFAULT '',
    gmail_thread_id  TEXT NOT NULL DEFAULT '',
    bewerbung_id     INTEGER REFERENCES bewerbungen(id),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);

CREATE TABLE IF NOT EXISTS email_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    direction        TEXT NOT NULL,
    gmail_message_id TEXT UNIQUE,
    gmail_thread_id  TEXT NOT NULL DEFAULT '',
    from_addr        TEXT NOT NULL DEFAULT '',
    to_addr          TEXT NOT NULL DEFAULT '',
    subject          TEXT NOT NULL DEFAULT '',
    snippet          TEXT NOT NULL DEFAULT '',
    internal_date    TEXT NOT NULL DEFAULT '',
    draft_id         INTEGER REFERENCES drafts(id),
    bewerbung_id     INTEGER REFERENCES bewerbungen(id),
    matched_by       TEXT NOT NULL DEFAULT '',
    classification   TEXT NOT NULL DEFAULT '',
    classified_by    TEXT NOT NULL DEFAULT '',
    needs_review     INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_log_bewerbung ON email_log(bewerbung_id);

CREATE TABLE IF NOT EXISTS status_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bewerbung_id  INTEGER NOT NULL REFERENCES bewerbungen(id),
    old_status    TEXT NOT NULL DEFAULT '',
    new_status    TEXT NOT NULL,
    source        TEXT NOT NULL,
    email_log_id  INTEGER REFERENCES email_log(id),
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status_history_bewerbung ON status_history(bewerbung_id);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _ensure_bewerbungen_columns(con: sqlite3.Connection) -> None:
    """Older legacy databases miss some columns — add them (additive only)."""
    existing = [row[1] for row in con.execute("PRAGMA table_info(bewerbungen)")]
    for col in ("email", "dokument"):
        if col not in existing:
            con.execute(f"ALTER TABLE bewerbungen ADD COLUMN {col} TEXT")


def _ensure_search_profile_columns(con: sqlite3.Connection) -> None:
    """Match-criteria columns added in schema v2 (additive only)."""
    existing = [row[1] for row in con.execute("PRAGMA table_info(search_profiles)")]
    for col, ddl in (
        ("hard_tags", "TEXT NOT NULL DEFAULT ''"),
        ("soft_preferences", "TEXT NOT NULL DEFAULT ''"),
        ("strictness", "INTEGER NOT NULL DEFAULT 50"),
    ):
        if col not in existing:
            con.execute(f"ALTER TABLE search_profiles ADD COLUMN {col} {ddl}")


def migrate(con: sqlite3.Connection) -> None:
    """Bring the database to the current schema. Safe to run repeatedly."""
    version = con.execute("PRAGMA user_version").fetchone()[0]
    con.execute(BEWERBUNGEN_SQL)
    _ensure_bewerbungen_columns(con)
    con.executescript(NEW_TABLES_SQL)
    _ensure_search_profile_columns(con)
    if version < 2:
        # v2 reserves match_score 0 for hard-criteria violations and hides
        # such rows by default. Under v1 semantics 0 just meant "very bad
        # fit", so remap pre-existing 0s to the new floor of 1 — otherwise
        # they would be silently hidden and mislabeled after the upgrade.
        con.execute("UPDATE jobs SET match_score=1 WHERE match_score=0")
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.commit()
