"""SQLite access layer: connection discipline, repositories, bootstrap.

Connections are short-lived (open, transact, close) and never shared
across threads or awaits. WAL mode lets UI reads proceed while background
pollers write; busy_timeout absorbs the rare write/write collision.
"""

import datetime
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from jobdeck import backup, config, migrations
from jobdeck.constants import EMAIL_OUTBOUND, STATUS_RANK
from jobdeck.dedupe import find_duplicate_bewerbung


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(db_path or config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    # No-op on an already-WAL database. The one-time delete→WAL conversion
    # needs an exclusive lock and fails fast under concurrency (the busy
    # handler is not consulted for it), so it must happen uncontended:
    # bootstrap migrates single-threaded at startup, and test fixtures
    # create their database through this function for the same reason.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def db(db_path: Path | None = None):
    """Short-lived connection: commits on success, rolls back on error."""
    con = connect(db_path)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# --------------------------------------------------------------------------
# Bootstrap: first-run import of the legacy database
# --------------------------------------------------------------------------
LEGACY_SETTINGS = Path("/data/Projects/bewerbung_update/bewerbung_settings.json")


def _find_legacy_db() -> Path | None:
    """Locate the legacy tracker's database for the one-time import."""
    candidates: list[Path] = []
    try:
        legacy = json.loads(LEGACY_SETTINGS.read_text(encoding="utf-8"))
        if legacy.get("db_folder"):
            candidates.append(Path(legacy["db_folder"]) / "bewerbungen.db")
    except (OSError, ValueError):
        pass
    candidates += [
        Path.home() / "Dropbox" / "Bewerbungen" / "bewerbungen.db",
        Path("/data/Projects/bewerbung_update/bewerbungen.db"),
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    return None


def bootstrap() -> str | None:
    """Prepare the data dir, import legacy data once, migrate, back up.

    Returns the backup system's data-loss warning, if any, so the UI can
    surface it at startup.
    """
    config.ensure_data_dirs()
    if not config.DB_PATH.exists():
        legacy = _find_legacy_db()
        if legacy is not None:
            # Consistent snapshot via the sqlite backup API — never a raw
            # file copy, the legacy DB may be open elsewhere.
            src = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
            try:
                dst = sqlite3.connect(config.DB_PATH)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
    with db() as con:
        migrations.migrate(con)
    return backup.run_startup_backup()


# --------------------------------------------------------------------------
# Applications (legacy `bewerbungen` table)
# --------------------------------------------------------------------------
def list_bewerbungen(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM bewerbungen ORDER BY gesendet_am DESC, id DESC"
    ).fetchall()


def get_bewerbung(con: sqlite3.Connection, row_id: int) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM bewerbungen WHERE id=?", (row_id,)).fetchone()


def add_bewerbung(con: sqlite3.Connection, values: dict) -> int:
    cur = con.execute(
        """
        INSERT INTO bewerbungen
            (gesendet_am, firma, email, ansprechpartner, strasse, plz_ort,
             kanal, status, notiz, dokument, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            values.get("gesendet_am", ""),
            values.get("firma", ""),
            values.get("email", ""),
            values.get("ansprechpartner", ""),
            values.get("strasse", ""),
            values.get("plz_ort", ""),
            values.get("kanal", ""),
            values.get("status", ""),
            values.get("notiz", ""),
            values.get("dokument", ""),
            _now(),
        ),
    )
    bewerbung_id = cur.lastrowid
    if values.get("status"):
        add_status_history(con, bewerbung_id, "", values["status"], source="user")
    return bewerbung_id


def update_bewerbung(con: sqlite3.Connection, row_id: int, values: dict) -> None:
    """Update editable fields. Status changes go through set_status()."""
    con.execute(
        """
        UPDATE bewerbungen SET
            gesendet_am=?, firma=?, email=?, ansprechpartner=?, strasse=?,
            plz_ort=?, kanal=?, notiz=?
        WHERE id=?
        """,
        (
            values.get("gesendet_am", ""),
            values.get("firma", ""),
            values.get("email", ""),
            values.get("ansprechpartner", ""),
            values.get("strasse", ""),
            values.get("plz_ort", ""),
            values.get("kanal", ""),
            values.get("notiz", ""),
            row_id,
        ),
    )


def delete_bewerbung(con: sqlite3.Connection, row_id: int) -> None:
    con.execute("DELETE FROM status_history WHERE bewerbung_id=?", (row_id,))
    con.execute(
        "UPDATE email_log SET bewerbung_id=NULL WHERE bewerbung_id=?", (row_id,)
    )
    con.execute("UPDATE jobs SET bewerbung_id=NULL WHERE bewerbung_id=?", (row_id,))
    con.execute(
        "UPDATE jobs SET duplicate_of=NULL WHERE duplicate_of=?", (row_id,)
    )
    con.execute("DELETE FROM bewerbungen WHERE id=?", (row_id,))


def set_dokument(con: sqlite3.Connection, row_id: int, path: str) -> None:
    con.execute("UPDATE bewerbungen SET dokument=? WHERE id=?", (path, row_id))


def set_status(
    con: sqlite3.Connection,
    bewerbung_id: int,
    new_status: str,
    source: str,
    email_log_id: int | None = None,
    note: str = "",
    force: bool = False,
) -> bool:
    """Change an application's status with a full audit trail.

    Automatic sources (reply classification) cannot downgrade a status —
    e.g. a late confirmation e-mail never overwrites a recorded invitation.
    Manual changes (source='user') always win. Returns True if applied.
    """
    row = get_bewerbung(con, bewerbung_id)
    if row is None:
        return False
    old = row["status"] or ""
    if old == new_status:
        return True
    automatic = source not in ("user", "reply_manual") and not force
    if automatic and STATUS_RANK.get(new_status, 0) < STATUS_RANK.get(old, 0):
        return False
    con.execute("UPDATE bewerbungen SET status=? WHERE id=?", (new_status, bewerbung_id))
    add_status_history(con, bewerbung_id, old, new_status, source, email_log_id, note)
    return True


def add_status_history(
    con: sqlite3.Connection,
    bewerbung_id: int,
    old_status: str,
    new_status: str,
    source: str,
    email_log_id: int | None = None,
    note: str = "",
) -> None:
    con.execute(
        """
        INSERT INTO status_history
            (bewerbung_id, old_status, new_status, source, email_log_id, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (bewerbung_id, old_status, new_status, source, email_log_id, note, _now()),
    )


def list_status_history(con: sqlite3.Connection, bewerbung_id: int) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM status_history WHERE bewerbung_id=? ORDER BY id DESC",
        (bewerbung_id,),
    ).fetchall()


def recent_activity(con: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT h.*, b.firma FROM status_history h
        JOIN bewerbungen b ON b.id = h.bewerbung_id
        ORDER BY h.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()


# --------------------------------------------------------------------------
# Search profiles
# --------------------------------------------------------------------------
def list_profiles(con: sqlite3.Connection, active_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM search_profiles"
    if active_only:
        sql += " WHERE active=1"
    return con.execute(sql + " ORDER BY id").fetchall()


def get_profile(con: sqlite3.Connection, profile_id: int) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM search_profiles WHERE id=?", (profile_id,)
    ).fetchone()


def add_profile(con: sqlite3.Connection, values: dict) -> int:
    cur = con.execute(
        """
        INSERT INTO search_profiles
            (name, keywords, location, radius_km, sources, active, auto_send,
             poll_interval_min, hard_tags, soft_preferences, strictness, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            values["name"],
            values["keywords"],
            values.get("location", ""),
            values.get("radius_km", 0),
            json.dumps(values.get("sources", ["arbeitsagentur", "jooble", "arbeitnow"])),
            int(values.get("active", 1)),
            int(values.get("auto_send", 0)),
            values.get("poll_interval_min", 60),
            values.get("hard_tags", ""),
            values.get("soft_preferences", ""),
            int(values.get("strictness", 50)),
            _now(),
        ),
    )
    return cur.lastrowid


def update_profile(con: sqlite3.Connection, profile_id: int, values: dict) -> None:
    con.execute(
        """
        UPDATE search_profiles SET
            name=?, keywords=?, location=?, radius_km=?, sources=?,
            active=?, auto_send=?, poll_interval_min=?,
            hard_tags=?, soft_preferences=?, strictness=?
        WHERE id=?
        """,
        (
            values["name"],
            values["keywords"],
            values.get("location", ""),
            values.get("radius_km", 0),
            json.dumps(values.get("sources", ["arbeitsagentur", "jooble", "arbeitnow"])),
            int(values.get("active", 1)),
            int(values.get("auto_send", 0)),
            values.get("poll_interval_min", 60),
            values.get("hard_tags", ""),
            values.get("soft_preferences", ""),
            int(values.get("strictness", 50)),
            profile_id,
        ),
    )


def delete_profile(con: sqlite3.Connection, profile_id: int) -> None:
    con.execute("UPDATE jobs SET profile_id=NULL WHERE profile_id=?", (profile_id,))
    con.execute("DELETE FROM search_profiles WHERE id=?", (profile_id,))


def mark_profile_polled(
    con: sqlite3.Connection, profile_id: int, error: str | None = None
) -> None:
    con.execute(
        "UPDATE search_profiles SET last_polled_at=?, last_poll_error=? WHERE id=?",
        (_now(), error, profile_id),
    )


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
def insert_job_if_new(con: sqlite3.Connection, values: dict) -> int | None:
    """Insert a discovered posting; returns its id or None if already known."""
    try:
        cur = con.execute(
            """
            INSERT INTO jobs
                (profile_id, source, external_id, title, company, location, remote,
                 url, description, contact_email, published_at, fetched_at, status,
                 duplicate_of)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values.get("profile_id"),
                values["source"],
                values["external_id"],
                values.get("title", ""),
                values.get("company", ""),
                values.get("location", ""),
                int(values.get("remote", 0)),
                values.get("url", ""),
                values.get("description", ""),
                values.get("contact_email", ""),
                values.get("published_at", ""),
                _now(),
                values.get("status", "new"),
                values.get("duplicate_of"),
            ),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # UNIQUE(source, external_id) — already known


# Score 0 is reserved for hard-criteria violations (see ai/scoring.py); the
# inbox hides those rows by default but they are never deleted.
MISMATCH_SQL = "match_score=0"


def list_jobs(
    con: sqlite3.Connection,
    status: str | None = None,
    limit: int = 500,
    mismatches: str = "include",
) -> list[sqlite3.Row]:
    """List postings. mismatches: 'include' (default), 'exclude' (hide the
    score-0 rows, NULL-safe so unscored postings stay visible) or 'only'
    (just the hidden pile — keeps mismatches reachable regardless of how
    many better-scored rows fill the page limit)."""
    where, params = [], []
    if status:
        where.append("status=?")
        params.append(status)
    if mismatches == "exclude":
        where.append("(match_score IS NULL OR match_score<>0)")
    elif mismatches == "only":
        where.append(MISMATCH_SQL)
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    order = "match_score DESC NULLS LAST, id DESC" if status else "id DESC"
    return con.execute(
        f"SELECT * FROM jobs{where_sql} ORDER BY {order} LIMIT ?",
        (*params, limit),
    ).fetchall()


def count_mismatches(con: sqlite3.Connection, status: str | None = None) -> int:
    """How many postings the mismatch filter would hide for this inbox view."""
    sql = f"SELECT COUNT(*) FROM jobs WHERE {MISMATCH_SQL}"
    params: tuple = ()
    if status:
        sql += " AND status=?"
        params = (status,)
    return con.execute(sql, params).fetchone()[0]


def get_job(con: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def set_job_status(
    con: sqlite3.Connection,
    job_id: int,
    status: str,
    bewerbung_id: int | None = None,
) -> None:
    if bewerbung_id is None:
        con.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
    else:
        con.execute(
            "UPDATE jobs SET status=?, bewerbung_id=? WHERE id=?",
            (status, bewerbung_id, job_id),
        )


def set_job_score(
    con: sqlite3.Connection, job_id: int, score: int, reason: str
) -> None:
    con.execute(
        "UPDATE jobs SET match_score=?, match_reason=? WHERE id=?",
        (score, reason, job_id),
    )


def set_job_contacts(con: sqlite3.Connection, job_id: int, contacts: dict) -> None:
    """Fill contact/reference columns from posting extraction.

    Only empty columns are filled — data the source API already delivered
    (e.g. arbeitsagentur contact_email) always wins over extraction.
    contact_source records 'posting' once anything was filled this way."""
    allowed = ("ansprechpartner", "contact_email", "contact_phone",
               "contact_strasse", "contact_plz_ort", "refnr")
    job = get_job(con, job_id)
    if job is None:
        return
    updates = {
        col: value.strip()
        for col, value in contacts.items()
        if col in allowed and value and value.strip() and not (job[col] or "").strip()
    }
    if not updates:
        return
    if not (job["contact_source"] or "").strip():
        updates["contact_source"] = "posting"
    assignments = ", ".join(f"{col}=?" for col in updates)  # closed allowlist
    con.execute(
        f"UPDATE jobs SET {assignments} WHERE id=?", (*updates.values(), job_id)
    )


def list_unscored_jobs(
    con: sqlite3.Connection, limit: int = 20, exclude_ids: set[int] | None = None
) -> list[sqlite3.Row]:
    """New postings that have not been match-scored yet, oldest first.

    exclude_ids skips postings the caller has given up on (retry cap), so
    they cannot starve the batch."""
    excluded = sorted(exclude_ids or ())
    extra = f" AND id NOT IN ({','.join('?' * len(excluded))})" if excluded else ""
    return con.execute(
        "SELECT * FROM jobs WHERE status='new' AND match_score IS NULL"
        + extra + " ORDER BY id LIMIT ?",
        (*excluded, limit),
    ).fetchall()


def count_jobs_by_status(con: sqlite3.Connection) -> dict[str, int]:
    rows = con.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
    return {row["status"]: row["n"] for row in rows}


# --------------------------------------------------------------------------
# Drafts (one per job — re-drafting replaces the previous attempt)
# --------------------------------------------------------------------------
def get_draft(con: sqlite3.Connection, draft_id: int) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()


def get_draft_by_job(con: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM drafts WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()


_DRAFT_FIELDS = ("status", "recipient", "betreff", "email_body",
                 "anschreiben_body", "pdf_path", "llm_model", "error")


def upsert_draft(con: sqlite3.Connection, job_id: int, values: dict) -> int:
    """Insert or update the job's single draft row. Returns the draft id.

    Updates touch only the keys present in `values`, so a status-only
    transition (claim, failure) never wipes previously drafted text."""
    existing = get_draft_by_job(con, job_id)
    if existing is None:
        fields = {field: values.get(field, "") for field in _DRAFT_FIELDS}
        fields["status"] = values.get("status", "generating")
        cur = con.execute(
            """
            INSERT INTO drafts
                (job_id, status, recipient, betreff, email_body,
                 anschreiben_body, pdf_path, llm_model, error,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, *(fields[f] for f in _DRAFT_FIELDS), _now(), _now()),
        )
        return cur.lastrowid
    updates = {field: values[field] for field in _DRAFT_FIELDS if field in values}
    columns = [*updates, "updated_at"]  # closed allowlist + timestamp
    assignments = ", ".join(f"{column}=?" for column in columns)
    con.execute(
        f"UPDATE drafts SET {assignments} WHERE id=?",
        (*updates.values(), _now(), existing["id"]),
    )
    return existing["id"]


def record_send(
    con: sqlite3.Connection,
    draft_id: int,
    gmail_message_id: str,
    gmail_thread_id: str,
    bewerbung_id: int | None,
) -> None:
    """Mark a draft as sent and link it to Gmail and the application row.

    Dedicated writer: the gmail/bewerbung columns are deliberately NOT in
    the upsert_draft allowlist — nothing else may ever set 'sent'."""
    con.execute(
        "UPDATE drafts SET status='sent', gmail_message_id=?, gmail_thread_id=?,"
        " bewerbung_id=?, error='', updated_at=? WHERE id=?",
        (gmail_message_id, gmail_thread_id, bewerbung_id, _now(), draft_id),
    )


def list_drafts_with_jobs(
    con: sqlite3.Connection, statuses: list[str]
) -> list[sqlite3.Row]:
    """Review-queue rows: drafts in the given statuses with their postings."""
    placeholders = ",".join("?" * len(statuses))
    return con.execute(
        f"""
        SELECT d.*, j.title AS job_title, j.company AS job_company,
               j.url AS job_url, j.match_score AS job_score,
               j.location AS job_location, j.status AS job_status,
               j.contact_email AS job_contact_email
        FROM drafts d JOIN jobs j ON j.id = d.job_id
        WHERE d.status IN ({placeholders})
        ORDER BY d.updated_at DESC, d.id DESC
        """,
        statuses,
    ).fetchall()


# --------------------------------------------------------------------------
# E-mail log (audit trail of every message the app sent or ingested)
# --------------------------------------------------------------------------
def add_email_log(con: sqlite3.Connection, values: dict) -> int:
    cur = con.execute(
        """
        INSERT INTO email_log
            (direction, gmail_message_id, gmail_thread_id, from_addr, to_addr,
             subject, snippet, internal_date, draft_id, bewerbung_id,
             matched_by, classification, classified_by, needs_review, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            values["direction"],
            values.get("gmail_message_id") or None,  # UNIQUE: '' would collide
            values.get("gmail_thread_id", ""),
            values.get("from_addr", ""),
            values.get("to_addr", ""),
            values.get("subject", ""),
            values.get("snippet", ""),
            values.get("internal_date", ""),
            values.get("draft_id"),
            values.get("bewerbung_id"),
            values.get("matched_by", ""),
            values.get("classification", ""),
            values.get("classified_by", ""),
            int(values.get("needs_review", 0)),
            _now(),
        ),
    )
    return cur.lastrowid


def count_outbound_today(con: sqlite3.Connection) -> int:
    """Sends since local midnight — the daily-cap meter (test sends count)."""
    today = datetime.date.today().isoformat()
    return con.execute(
        "SELECT COUNT(*) FROM email_log WHERE direction LIKE ? AND created_at LIKE ?",
        (f"{EMAIL_OUTBOUND}%", f"{today}%"),
    ).fetchone()[0]


def count_outbound_for_draft(con: sqlite3.Connection, draft_id: int) -> int:
    """Real (non-test) sends already recorded for this draft."""
    return con.execute(
        "SELECT COUNT(*) FROM email_log WHERE draft_id=? AND direction=?",
        (draft_id, EMAIL_OUTBOUND),
    ).fetchone()[0]


# --------------------------------------------------------------------------
# Application creation from a job (manual portal flow / after send)
# --------------------------------------------------------------------------
def apply_job(
    con: sqlite3.Connection,
    job_id: int,
    kanal: str,
    status: str = "Gesendet",
    dokument: str = "",
    notiz_extra: str = "",
) -> int | None:
    """Record an application for a job posting. Returns the bewerbung id,
    or None if a duplicate application blocks it."""
    job = get_job(con, job_id)
    if job is None:
        return None
    dup = find_duplicate_bewerbung(con, job["company"], job["contact_email"])
    if dup is not None:
        set_job_status(con, job_id, "duplicate")
        con.execute("UPDATE jobs SET duplicate_of=? WHERE id=?", (dup["id"], job_id))
        return None
    notiz = job["url"]
    if notiz_extra:
        notiz = f"{notiz_extra} | {notiz}"
    bewerbung_id = add_bewerbung(
        con,
        {
            "gesendet_am": datetime.date.today().isoformat(),
            "firma": job["company"],
            "email": job["contact_email"],
            "kanal": kanal,
            "status": status,
            "notiz": notiz,
            "dokument": dokument,
        },
    )
    set_job_status(con, job_id, "applied", bewerbung_id=bewerbung_id)
    return bewerbung_id


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def get_setting(con: sqlite3.Connection, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def ai_enabled(con: sqlite3.Connection) -> bool:
    """Master switch for all LLM spend. Off by default — the user opts in
    from Settings; every service that calls the LLM must check this first."""
    return get_setting(con, "ai_enabled", "0") == "1"


def _bump_int_setting(con: sqlite3.Connection, key: str, delta: int) -> None:
    con.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = "
        "CAST(CAST(value AS INTEGER) + CAST(excluded.value AS INTEGER) AS TEXT)",
        (key, str(int(delta))),
    )


def record_llm_usage(
    con: sqlite3.Connection, input_tokens: int, output_tokens: int, cost_usd: float
) -> None:
    """Accumulate LLM metering counters (settings values are strings).

    The arithmetic happens in SQL so concurrent writers (a scoring batch
    and a drafting click) cannot lose updates to a read-modify-write race."""
    _bump_int_setting(con, "llm_calls", 1)
    _bump_int_setting(con, "llm_input_tokens", input_tokens)
    _bump_int_setting(con, "llm_output_tokens", output_tokens)
    con.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, printf('%.6f', ?)) "
        "ON CONFLICT(key) DO UPDATE SET value = "
        "printf('%.6f', CAST(value AS REAL) + CAST(excluded.value AS REAL))",
        ("llm_cost_usd", float(cost_usd)),
    )
