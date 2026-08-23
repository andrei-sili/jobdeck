"""Database schema management.

Migrations are strictly additive: new tables and new columns only. The
legacy `bewerbungen` table keeps its exact shape so the historical data
(and any legacy tooling still reading it) continues to work unchanged.
"""

import datetime
import pathlib
import sqlite3

from jobdeck import constants, dates

SCHEMA_VERSION = 13

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
    ansprechpartner  TEXT NOT NULL DEFAULT '',
    contact_phone    TEXT NOT NULL DEFAULT '',
    contact_strasse  TEXT NOT NULL DEFAULT '',
    contact_plz_ort  TEXT NOT NULL DEFAULT '',
    contact_source   TEXT NOT NULL DEFAULT '',
    refnr            TEXT NOT NULL DEFAULT '',
    published_on        TEXT NOT NULL DEFAULT '',
    liveness            TEXT NOT NULL DEFAULT '',
    liveness_checked_at TEXT NOT NULL DEFAULT '',
    work_strasse        TEXT NOT NULL DEFAULT '',
    work_plz_ort        TEXT NOT NULL DEFAULT '',
    salary_from         TEXT NOT NULL DEFAULT '',
    salary_to           TEXT NOT NULL DEFAULT '',
    salary_period       TEXT NOT NULL DEFAULT '',
    temp_agency         INTEGER NOT NULL DEFAULT 0,
    bookmarked_at       TEXT NOT NULL DEFAULT '',
    opened_at           TEXT NOT NULL DEFAULT '',
    form_opened_at      TEXT NOT NULL DEFAULT '',
    upload_path         TEXT NOT NULL DEFAULT '',
    mappe_kind          TEXT NOT NULL DEFAULT '',
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
    sending_test     INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
-- The job inbox asks "what is THIS posting's draft doing" once per row, and
-- the grouped view pays that query three times per refresh (count, list,
-- siblings). Without this the correlated subquery scans drafts per job row:
-- measured 3000 jobs x 1000 drafts at 93 ms against 28 ms with the index.
CREATE INDEX IF NOT EXISTS idx_drafts_job_id ON drafts(job_id);

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
    body_text        TEXT NOT NULL DEFAULT '',
    job_id           INTEGER REFERENCES jobs(id),
    matched_note     TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_log_bewerbung ON email_log(bewerbung_id);
-- Reply ingestion joins an inbound message's threadId against the outbound
-- rows; without this the join scans the whole log once per new message.
CREATE INDEX IF NOT EXISTS idx_email_log_thread ON email_log(gmail_thread_id);

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

-- What a letter is allowed to claim (schema v9). One row is one permission:
-- a competence or credential (`fact`) together with the ONE project or
-- employer it belongs to (`binding`). The pair is the point — the drafting
-- rule that keeps being broken is not inventing a skill, it is welding a
-- real skill to the wrong project, and only the pair can state that.
--
-- `terms` are the words a letter would have to use to be claiming this, and
-- exist so the register can COUNT its own use across written letters rather
-- than merely assert itself.
CREATE TABLE IF NOT EXISTS claims (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fact       TEXT NOT NULL,
    binding    TEXT NOT NULL DEFAULT '',
    terms      TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Companies he never wants to see again (schema v13). Keyed by jd_norm of the
-- company name — the SAME normalisation the grouping and the duplicate gate
-- use, so a spelling variant cannot slip past the one he excluded.
--
-- Not a column on `jobs`, deliberately. A column would have to be written for
-- every posting that exists AND every posting that arrives, which is a
-- backfill plus an invariant to maintain at each insert; the filter is
-- evaluated at read time, so hiding a company reaches postings discovered
-- next week without anything remembering to write to them.
--
-- `company` keeps the spelling that was on screen when he pressed, because the
-- key is normalised and unreadable — the "Ausgeblendete Firmen" view has to
-- show him a name he recognises.
-- `id` exists only so the signature has something that never repeats.
-- AUTOINCREMENT, deliberately: without it SQLite assigns max(rowid)+1 over the
-- rows PRESENT, so releasing the newest hidden company and hiding another
-- lands on the same number — the exact pair the undo bar produces, and the
-- screen would stay pruned.
CREATE TABLE IF NOT EXISTS hidden_companies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_key TEXT NOT NULL UNIQUE,
    company     TEXT NOT NULL,
    hidden_at   TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'user'
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


def _ensure_job_contact_columns(con: sqlite3.Connection) -> None:
    """Contact/reference columns added in schema v3 (additive only).

    Filled by the extraction that rides the scoring call; the cascade slice
    (web enrichment) will reuse them with other contact_source values."""
    existing = [row[1] for row in con.execute("PRAGMA table_info(jobs)")]
    for col in ("ansprechpartner", "contact_phone", "contact_strasse",
                "contact_plz_ort", "contact_source", "refnr"):
        if col not in existing:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


def _ensure_apply_channel_columns(con: sqlite3.Connection) -> None:
    """Apply-channel columns added in schema v5 (additive only).

    Filled by the deterministic apply-channel classifier (apply_channel.py):
    where one applies (direct e-mail / ATS portal / board / company site), the
    ATS vendor label when known, and the resolved final apply URL (after
    following an aggregator redirect). Auto-send stays gated to direct_email."""
    existing = [row[1] for row in con.execute("PRAGMA table_info(jobs)")]
    for col in ("apply_channel", "ats_vendor", "apply_url"):
        if col not in existing:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


def _ensure_freshness_columns(con: sqlite3.Connection) -> None:
    """Freshness/liveness columns added in schema v6 (additive only).

    `published_on` is the ISO date DERIVED from the source's own
    `published_at`, which stays exactly as the board sent it (three formats,
    one of them Unix epoch — see dates.parse_posting_date). The derived column
    is what SQL can order and compare; keeping both means a re-read of the raw
    value is always possible.

    `liveness` is '', 'alive' or 'gone' as last observed, with the timestamp of
    that observation. A posting whose ad is gone is HIDDEN by default and never
    deleted."""
    existing = [row[1] for row in con.execute("PRAGMA table_info(jobs)")]
    for col in ("published_on", "liveness", "liveness_checked_at"):
        if col not in existing:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


def _ensure_form_flow_columns(con: sqlite3.Connection) -> None:
    """Where a form application stands, as facts rather than as a status
    (schema v10).

    * `form_opened_at`: the moment he opened an employer's form. This replaces
      the status `portal`, and the replacement is the point: `portal` is a
      status, so every view that pins a concrete one hid the posting he had
      just started working on — which is exactly the complaint. A timestamp
      hides nothing, and it is independent of `status` for the same reason
      `opened_at` is: starting an application is a different question from what
      became of the posting.
    * `upload_path`: the copy staged in `config.UPLOAD_DIR` for an employer's
      file picker. Deliberately NOT called mappe_path — the Mappe itself lives
      on `drafts.pdf_path` and is archived under `output/job_<id>/`; this is a
      second, transient artifact with a different lifetime, and giving it the
      Mappe's name is how two columns start disagreeing about one file.
    * `mappe_kind`: what the legacy build produced, written by the build and
      never inferred from the file system. Empty means no complete legacy
      package is staged. The target document model is defined separately in
      `docs/adr/0005-job-specific-application-documents.md`.
    """
    existing = [row[1] for row in con.execute("PRAGMA table_info(jobs)")]
    for col in ("form_opened_at", "upload_path", "mappe_kind"):
        if col not in existing:
            con.execute(
                f"ALTER TABLE jobs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


def _restate_portal_as_a_moment(con: sqlite3.Connection) -> None:
    """Re-represent the postings whose form was already open (v10, once).

    Not a status move, so not `set_job_status`: the fact does not change, only
    where it is written. Each such posting goes back to `new` — it never left
    the working list in any sense he chose — and gains the timestamp the rest
    of the app now reads.

    The age is taken from evidence and never from the clock: the newest draft's
    `updated_at`, else `opened_at`. A posting with neither gets the literal
    sentinel and reads "seit unbekannt". Stamping `now` here would be inventing
    an age for eleven real rows, which is precisely the thing that must not
    happen — the strip would then sort them as if they had just been started.
    """
    con.execute(
        "UPDATE jobs SET status='new', form_opened_at=COALESCE("
        "  (SELECT MAX(d.updated_at) FROM drafts d"
        "    WHERE d.job_id=jobs.id AND d.updated_at<>''),"
        "  NULLIF(opened_at, ''), ?) "
        "WHERE status='portal'",
        (constants.FORM_OPENED_UNKNOWN,),
    )


def _backfill_published_on(con: sqlite3.Connection) -> None:
    """Derive `published_on` for every posting that still lacks one.

    Runs on every migration rather than once at v6: it only ever fills a blank
    from data already in the row, so it is both idempotent and self-healing —
    a posting whose format the parser learns to read later gets its date on the
    next start, with no second migration."""
    existing = [row[1] for row in con.execute("PRAGMA table_info(jobs)")]
    if "published_at" not in existing:
        # a jobs table old enough to predate the column (nothing adds it —
        # only fresh databases get the full definition); there is no raw value
        # to derive from, so there is nothing to do
        return
    rows = con.execute(
        "SELECT id, published_at FROM jobs "
        "WHERE published_on='' AND published_at<>''"
    ).fetchall()
    for job_id, published_at in rows:
        iso = dates.posting_date_iso(published_at)
        if iso:
            con.execute(
                "UPDATE jobs SET published_on=? WHERE id=?", (iso, job_id)
            )


def _backfill_application_documents(con: sqlite3.Connection) -> None:
    """Point an application at the Mappe that was actually built for it.

    Two writers used to record a form application and only one of them passed
    `dokument`, so 13 of his 35 Online-Portal ledger rows say no document
    exists while the PDF sits under output/job_<id>/. One recorder makes that
    impossible going forward; this fills in what the disagreement already cost.

    Repeated on every start rather than gated on a version, in the shape of
    `_backfill_published_on`: it only ever fills a BLANK from a path the app
    itself stored, so it is idempotent and self-healing — a row whose PDF is
    rebuilt later gets its pointer on the next start.

    The file must still exist. A ledger entry naming a path that is not there
    is worse than one that admits it has nothing: he would click it.
    """
    job_cols = [row[1] for row in con.execute("PRAGMA table_info(jobs)")]
    draft_cols = [row[1] for row in con.execute("PRAGMA table_info(drafts)")]
    if "bewerbung_id" not in job_cols or "pdf_path" not in draft_cols:
        # a database old enough to predate either column — nothing to derive
        # from, and a migration must never raise on a shape it can simply skip
        return
    rows = con.execute(
        "SELECT b.id, d.pdf_path FROM bewerbungen b "
        "  JOIN jobs j ON j.bewerbung_id = b.id "
        "  JOIN drafts d ON d.job_id = j.id "
        " WHERE COALESCE(b.dokument,'')='' AND COALESCE(d.pdf_path,'')<>''"
    ).fetchall()
    for bewerbung_id, pdf_path in rows:
        if pathlib.Path(pdf_path).is_file():
            con.execute("UPDATE bewerbungen SET dokument=? WHERE id=?",
                        (pdf_path, bewerbung_id))


def _file_letters_of_recorded_applications(con: sqlite3.Connection) -> None:
    """Close the letters whose application is already in the ledger.

    Until now recording a form application left its letter at 'ready', so it
    went on waiting in the Postausgang for ever. Twelve of the seventeen
    letters in his queue were of that kind — each offering to be e-mailed to a
    company he had applied to hours earlier, one press away from a second
    application at a firm that already has one.

    Only where the POSTING carries the application itself (`jobs.bewerbung_id`)
    — that is the pair whose letter provably went out together. A letter at a
    company reached through some OTHER posting is left exactly where it is:
    the queue already warns on that row and the send gate refuses it, and
    filing it would claim an employer holds a letter nobody ever sent.

    Run ONCE, at the upgrade, unlike the two backfills above — they only ever
    fill a blank nothing else writes, while 'ready' is a state a draft comes
    BACK to. Repeated every start this would stop healing history and start
    governing the future: restoring a discarded letter, or re-writing one, on
    a posting already applied to would be silently frozen again at the next
    launch, with no notice and no way out of 'filed'.

    Only the NEWEST draft of a posting, which is the one `get_draft_by_job`
    and therefore `apply_record` mean by "the letter" — `drafts.job_id` has no
    UNIQUE constraint, and filing all of them would make the migration and the
    running app disagree about how many letters an application carried.

    And only where the application row really exists: `drafts.bewerbung_id` is
    a foreign key with `PRAGMA foreign_keys=ON`, so a single dangling
    `jobs.bewerbung_id` would raise IntegrityError out of `migrate()` — which
    runs at startup, before any screen, so it would not be a broken screen but
    an app that cannot open at all.
    """
    draft_cols = [row[1] for row in con.execute("PRAGMA table_info(drafts)")]
    job_cols = [row[1] for row in con.execute("PRAGMA table_info(jobs)")]
    if "bewerbung_id" not in draft_cols or "bewerbung_id" not in job_cols:
        return  # too old to know which application belongs to which posting
    con.execute(
        """
        UPDATE drafts SET status='filed', updated_at=?, bewerbung_id=(
                SELECT j.bewerbung_id FROM jobs j WHERE j.id = drafts.job_id)
         WHERE status IN ('ready', 'approved')
           AND id = (SELECT d2.id FROM drafts d2
                      WHERE d2.job_id = drafts.job_id
                      ORDER BY d2.id DESC LIMIT 1)
           AND EXISTS (SELECT 1 FROM jobs j
                        JOIN bewerbungen b ON b.id = j.bewerbung_id
                       WHERE j.id = drafts.job_id)
        """,
        (datetime.datetime.now().isoformat(timespec="seconds"),),
    )


def _ensure_source_fact_columns(con: sqlite3.Connection) -> None:
    """Facts the boards state and the app used to throw away (schema v7).

    All of them come from the Arbeitsagentur detail payload, which the app
    already fetches twice — once at discovery and again on every liveness probe:

    * `work_strasse` / `work_plz_ort`: the street-level address of the work
      location. The letter's {{STRASSE}} / {{PLZ_ORT}} tokens were filled only
      from an address an LLM had read out of the posting prose, which 725 of his
      769 postings simply do not state.
    * `salary_from` / `salary_to` / `salary_period`: the stated pay range, kept
      as the board sent it so the inbox can show it without re-deriving.
    * `temp_agency`: Arbeitnehmerüberlassung. A posting where the employer is a
      staffing firm and the work happens somewhere else — worth knowing before
      writing an application, and it is why the work address above is only ever
      a FALLBACK for the letter.
    """
    existing = [row[1] for row in con.execute("PRAGMA table_info(jobs)")]
    for col in ("work_strasse", "work_plz_ort", "salary_from", "salary_to",
                "salary_period"):
        if col not in existing:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    if "temp_agency" not in existing:
        con.execute(
            "ALTER TABLE jobs ADD COLUMN temp_agency INTEGER NOT NULL DEFAULT 0")


def _ensure_reading_columns(con: sqlite3.Connection) -> None:
    """What he has done with a posting BEFORE deciding anything (schema v8).

    Both are timestamps rather than flags, and both are independent of
    `status` — acting on a posting is a different question from having looked
    at it or having put it on a shelf.

    * `bookmarked_at`: set aside to come back to. The pile he builds by hand is
      the one place where his own order matters, and "when did I mark this" is
      the only thing that can order it; score decides everywhere else.
    * `opened_at`: the first time he actually read it. Without this "new" can
      only mean "recently discovered", which is a fact about the board rather
      than about him — and the whole point of a list shaped like an inbox is
      that it can tell him what he has not looked at yet.

    Empty string means not-yet, so both read as booleans where that is all a
    query needs.
    """
    existing = [row[1] for row in con.execute("PRAGMA table_info(jobs)")]
    for col in ("bookmarked_at", "opened_at"):
        if col not in existing:
            con.execute(
                f"ALTER TABLE jobs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


def _ensure_email_log_columns(con: sqlite3.Connection) -> None:
    """Reply-ingestion columns added in schema v12 (additive only).

    * `body_text`: the extracted plain text of a MATCHED inbound message,
      capped by the ingestion service. Stored so the review row can show him
      what he is classifying without re-reading Gmail — and only for mail the
      app could tie to an application; unmatched mail is never stored at all.
    * `job_id`: an Eingangsbestätigung matches a POSTING on the Läuft strip —
      at that moment no bewerbungen row exists yet, so the reply columns'
      `bewerbung_id` cannot carry the link. Replies to sent applications keep
      using `bewerbung_id`; a receipt row may carry both once recorded.
    * `matched_note`: WHY this row says what it says — the evidence that
      identified a posting ("Absender firma.de"), or the model's one German
      sentence when the model was the one that proposed. Both were computed
      (and, for the model, paid for) and then thrown away, leaving a
      proposal on screen with nothing to judge it by.
    """
    existing = [row[1] for row in con.execute("PRAGMA table_info(email_log)")]
    if "body_text" not in existing:
        con.execute(
            "ALTER TABLE email_log ADD COLUMN body_text TEXT NOT NULL DEFAULT ''")
    if "job_id" not in existing:
        con.execute(
            "ALTER TABLE email_log ADD COLUMN job_id INTEGER REFERENCES jobs(id)")
    if "matched_note" not in existing:
        con.execute("ALTER TABLE email_log ADD COLUMN matched_note "
                    "TEXT NOT NULL DEFAULT ''")


def _ensure_draft_columns(con: sqlite3.Connection) -> None:
    """Send-tracking columns added in schema v4 (additive only).

    sending_test records whether an in-flight claim is a test send, so a
    stuck one can never be resolved into a real application record."""
    existing = [row[1] for row in con.execute("PRAGMA table_info(drafts)")]
    if "sending_test" not in existing:
        con.execute(
            "ALTER TABLE drafts ADD COLUMN sending_test INTEGER NOT NULL DEFAULT 0"
        )


def migrate(con: sqlite3.Connection) -> None:
    """Bring the database to the current schema. Safe to run repeatedly."""
    version = con.execute("PRAGMA user_version").fetchone()[0]
    con.execute(BEWERBUNGEN_SQL)
    _ensure_bewerbungen_columns(con)
    con.executescript(NEW_TABLES_SQL)
    _ensure_search_profile_columns(con)
    _ensure_job_contact_columns(con)
    _ensure_apply_channel_columns(con)
    _ensure_freshness_columns(con)
    _ensure_source_fact_columns(con)
    _ensure_reading_columns(con)
    _ensure_form_flow_columns(con)
    _ensure_draft_columns(con)
    _ensure_email_log_columns(con)
    _backfill_published_on(con)
    _backfill_application_documents(con)
    if version < 11:
        # ONCE, at the upgrade — see the function's own docstring: 'ready' is a
        # state a draft returns to, so a backfill that ran every start would
        # freeze a letter he had deliberately restored or re-written.
        _file_letters_of_recorded_applications(con)
    if version < 10:
        _restate_portal_as_a_moment(con)
    if version < 2:
        # v2 reserves match_score 0 for hard-criteria violations and hides
        # such rows by default. Under v1 semantics 0 just meant "very bad
        # fit", so remap pre-existing 0s to the new floor of 1 — otherwise
        # they would be silently hidden and mislabeled after the upgrade.
        con.execute("UPDATE jobs SET match_score=1 WHERE match_score=0")
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.commit()
