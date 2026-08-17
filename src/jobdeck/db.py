"""SQLite access layer: connection discipline, repositories, bootstrap.

Connections are short-lived (open, transact, close) and never shared
across threads or awaits. WAL mode lets UI reads proceed while background
pollers write; busy_timeout absorbs the rare write/write collision.
"""

import datetime
import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from jobdeck import apply_channel, backup, config, dates, freshness, migrations
from jobdeck.constants import (
    BEANTWORTET_STATUS,
    DEFAULT_DAILY_CAP,
    DEFAULT_DAILY_DRAFT_CAP,
    DRAFT_STATUS,
    EMAIL_INBOUND,
    EMAIL_OUTBOUND,
    EMAIL_OUTBOUND_TEST,
    FORM_OPENED_UNKNOWN,
    LIVENESS_GONE,
    STATUS_RANK,
)
from jobdeck.dedupe import find_duplicate_bewerbung, norm

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _hours_ago(hours: int) -> str:
    """A `_now()`-comparable timestamp, `hours` in the past."""
    moment = datetime.datetime.now() - datetime.timedelta(hours=hours)
    return moment.isoformat(timespec="seconds")


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
    # The duplicate gate compares companies in Python with str.casefold(),
    # because SQLite's own lower() folds ASCII only (see dedupe.py). SQL that
    # groups by company must use the SAME function or it would tell the user
    # "one application per company" while disagreeing with the gate that
    # enforces it — and would miss "MÜLLER" vs "Müller" while doing so.
    con.create_function("jd_norm", 1, norm, deterministic=True)
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
        # Self-healing, like the published_on backfill beside it: derived from
        # data the row already holds, so it is idempotent and a posting whose
        # e-mail was harvested before this rule existed stops looking like a
        # form job on the next start.
        converted = resolve_email_channels(con)
        if converted:
            log.info("apply channel: %s postings apply by e-mail", converted)
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
    """Remove an application and every reference to it.

    A FILED letter is handed back, a SENT one is not, and the difference is
    what evidence survives the deletion. A sent letter has a Gmail message id:
    it really left, whatever the ledger says afterwards, and 'sent' stays true.
    A filed letter's only evidence that it reached anyone IS this row — so
    deleting the row and leaving the letter at 'filed' would strand it in a
    state that cannot be sent, cannot be discarded and cannot be re-written,
    for an application the user has just said did not happen.
    """
    con.execute("DELETE FROM status_history WHERE bewerbung_id=?", (row_id,))
    con.execute(
        "UPDATE email_log SET bewerbung_id=NULL WHERE bewerbung_id=?", (row_id,)
    )
    unfile_draft(con, row_id)
    con.execute(
        "UPDATE drafts SET bewerbung_id=NULL WHERE bewerbung_id=?", (row_id,)
    )
    # …and the posting goes back to being a posting. Clearing the link while
    # leaving `status='applied'` hid it from every working view with no ledger
    # row behind it and no way back — the same shape `unrecord_application`
    # was written to avoid, on the other of the two paths that delete a row.
    con.execute(
        "UPDATE jobs SET bewerbung_id=NULL, status='new' WHERE bewerbung_id=?",
        (row_id,),
    )
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
# Claims — what a letter is allowed to claim (schema v9)
# --------------------------------------------------------------------------
def list_claims(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """The register in the order it is read. `id` breaks ties so two claims
    given the same rank never swap places between two renders."""
    return con.execute(
        "SELECT * FROM claims ORDER BY sort_order, id").fetchall()


def add_claim(con: sqlite3.Connection, values: dict) -> int:
    """Append a permission. A new row sorts after every existing one unless
    the caller places it, so adding never reorders what is already there."""
    order = values.get("sort_order")
    if order is None:
        order = (con.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM claims"
        ).fetchone()[0])
    stamp = _now()
    cur = con.execute(
        """
        INSERT INTO claims (fact, binding, terms, sort_order, created_at,
                            updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (values["fact"].strip(), values.get("binding", "").strip(),
         values.get("terms", "").strip(), int(order), stamp, stamp),
    )
    return cur.lastrowid


def update_claim(con: sqlite3.Connection, claim_id: int, values: dict) -> None:
    con.execute(
        "UPDATE claims SET fact=?, binding=?, terms=?, updated_at=? WHERE id=?",
        (values["fact"].strip(), values.get("binding", "").strip(),
         values.get("terms", "").strip(), _now(), claim_id),
    )


def delete_claim(con: sqlite3.Connection, claim_id: int) -> None:
    con.execute("DELETE FROM claims WHERE id=?", (claim_id,))


def letter_bodies(con: sqlite3.Connection) -> list[str]:
    """Every Anschreiben this app has written, for the register's counters.

    Every draft counts, not only the sent ones: a claim that keeps being
    written and then discarded is exactly what the register should show, and
    reading only sent letters would call it unused.
    """
    return [row[0] for row in con.execute(
        "SELECT anschreiben_body FROM drafts WHERE TRIM(anschreiben_body) <> ''"
    )]


def claims_signature(con: sqlite3.Connection) -> tuple:
    """What the register shows: its own rows, and the letters it counts.

    The claims are compared VERBATIM rather than through their timestamps.
    `updated_at` has second resolution, so correcting a claim's terms and
    looking at the counter it changes — the whole loop this screen exists for
    — happens well inside one tick and would leave the screen certain it was
    already current. The register is a handful of short rows, so the cheap
    aggregate is the one that would hide the edit.

    The letters cannot be compared that way (thousands of characters each),
    so their total length stands in for their content: a body is rewritten by
    drafting or edited by hand, and either changes both the count of letters
    and, in all but a same-length coincidence, this sum — which the timestamp
    then catches on the next second.
    """
    return tuple(con.execute(
        "SELECT (SELECT COUNT(*) FROM claims), "
        "(SELECT GROUP_CONCAT(fact || '|' || binding || '|' || terms, '§') "
        " FROM (SELECT * FROM claims ORDER BY sort_order, id)), "
        "(SELECT COUNT(*) FROM drafts WHERE TRIM(anschreiben_body) <> ''), "
        "(SELECT COALESCE(SUM(LENGTH(anschreiben_body)), 0) FROM drafts), "
        "(SELECT MAX(COALESCE(updated_at,'')) FROM drafts)"
    ).fetchone())


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
                 url, description, contact_email, published_at, published_on,
                 fetched_at, status, duplicate_of)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                # the board's raw value is kept verbatim; the ISO form beside
                # it is what SQL can order on (three source formats, one of
                # them Unix epoch — see dates.parse_posting_date)
                dates.posting_date_iso(values.get("published_at", "")),
                _now(),
                values.get("status", "new"),
                values.get("duplicate_of"),
            ),
        )
        job_id = cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # UNIQUE(source, external_id) — already known
    # A posting that arrives WITH an application address is an e-mail job from
    # the first second; waiting for a resolve pass to say so is what left 81 of
    # them looking like form jobs.
    resolve_email_channels(con, job_id)
    return job_id


# Score 0 is reserved for hard-criteria violations (see ai/scoring.py); the
# inbox hides those rows by default but they are never deleted.
MISMATCH_SQL = "match_score=0"

# A posting whose ad the source says is no longer there (services/liveness.py).
# Hidden by default for the same reason and with the same promise: a 404 is a
# fact, not a judgement — it hides the row, it never deletes it.
GONE_SQL = f"liveness='{LIVENESS_GONE}'"

# What the posting's own draft is doing, so the inbox can say so on the row.
# A draft that is being written is otherwise invisible EVERYWHERE for the
# minute it takes: the row exists, and no view showed it, so the only feedback
# a second press gave was "already being generated".
# `drafts.job_id` carries no UNIQUE constraint and `get_draft_by_job` answers
# with the newest row — this must pick the SAME one, or the inbox would
# describe a draft other than the one every button acts on.
_DRAFT_STATUS_SQL = (
    "(SELECT d.status FROM drafts d WHERE d.job_id=jobs.id "
    "ORDER BY d.id DESC LIMIT 1)"
)
# When the draft last moved. For a 'generating' row this is when the claim was
# taken, which is what tells a draft still being written from one whose process
# died — the Draft button is the only thing that can restart the second, so the
# inbox has to be able to tell them apart (services/drafting.claim_is_stale).
_DRAFT_UPDATED_SQL = (
    "(SELECT d.updated_at FROM drafts d WHERE d.job_id=jobs.id "
    "ORDER BY d.id DESC LIMIT 1)"
)
# The Mappe this posting already has. A screen that offers to build one has to
# know whether it exists, and on the form path the PDF is the thing he uploads.
_DRAFT_PDF_SQL = (
    "(SELECT d.pdf_path FROM drafts d WHERE d.job_id=jobs.id "
    "ORDER BY d.id DESC LIMIT 1)"
)


# A posting at a company an application already went to. Only ONE application
# per company is possible, so such a posting can never become one — the same
# kind of fact as a score-0 mismatch or an offline ad, and hidden on the same
# terms. It is the SQL mirror of dedupe._first_match (company OR contact
# e-mail, each arm requiring the posting's own field to be non-empty), because
# the filter has to run where the paging and the counts do. A differential
# test pins the two equal over a generated corpus — two hand-written copies of
# one rule drift, and this one decides whether he sees a posting at all.
# Written as two UNCORRELATED IN-subqueries on purpose. The obvious
# `EXISTS (... WHERE jd_norm(b.firma) = jd_norm(jobs.company))` makes SQLite
# call jd_norm — a Python callback — once per (posting, application) pair:
# measured at 330 ms over the real corpus, and the inbox pays the filter three
# times per page (count, list, siblings). Uncorrelated, each side is folded
# once and matched through an ephemeral index.
APPLIED_FIRM_SQL = """(
    (jd_norm(jobs.company) <> '' AND jd_norm(jobs.company) IN (
        SELECT jd_norm(b.firma) FROM bewerbungen b WHERE jd_norm(b.firma) <> ''))
 OR (jd_norm(jobs.contact_email) <> '' AND jd_norm(jobs.contact_email) IN (
        SELECT jd_norm(b.email) FROM bewerbungen b WHERE jd_norm(b.email) <> '')))"""


# A posting whose application is UNDER WAY. Two ways that happens and they are
# one fact from his side: a draft is being written or waiting to be sent, or he
# has opened the employer's form. A screen that showed only the first would lose
# every form application between opening the form and recording it — which is
# most of them.
#
# The form arm reads the MOMENT (`form_opened_at`) rather than the old `portal`
# status, and that is strictly more coverage, not a rename: a form opened before
# any letter was written had no draft to be found by the second arm and no
# status of its own once `portal` was retired, so this view used to lose exactly
# the applications it exists to show.
#
# The draft statuses are listed rather than negated so a NEW one has to be
# classified deliberately: silently counting an unknown draft state as work in
# progress is how a screen starts lying.
OPEN_DRAFT_STATUSES = ("generating", "ready", "failed", "approved", "sending")
_IN_PROGRESS_SQL = (
    "(jobs.form_opened_at<>'' OR EXISTS ("
    "SELECT 1 FROM drafts d WHERE d.job_id=jobs.id AND d.status IN ("
    + ",".join("?" * len(OPEN_DRAFT_STATUSES)) + ")))"
)


def _job_filters(
    status: str | None, mismatches: str, gone: str, applied: str = "include",
    old: str = "include", stale_age_days: int = freshness.DEFAULT_STALE_AGE_DAYS,
    bookmarked: str = "include", opened: str = "include",
    in_progress: str = "include", search: str = "",
    keep_ids: tuple[int, ...] = (),
) -> tuple[list[str], list]:
    """WHERE fragments + bound values shared by the list and the count, so a
    page can never be filtered differently from the total printed beside it.

    An unrecognised filter value raises rather than being ignored: silently
    falling through would SHOW a pile the caller asked to hide, and a hidden
    pile exists precisely because its rows should not be acted on."""
    for name, value in (("mismatches", mismatches), ("gone", gone),
                        ("applied", applied), ("old", old),
                        ("bookmarked", bookmarked), ("opened", opened),
                        ("in_progress", in_progress)):
        if value not in ("include", "exclude", "only"):
            raise ValueError(f"{name}={value!r}: expected include/exclude/only")
    where, params = [], []
    if status:
        where.append("status=?")
        params.append(status)
    if mismatches == "exclude":
        where.append("(match_score IS NULL OR match_score<>0)")
    elif mismatches == "only":
        where.append(MISMATCH_SQL)
    if gone == "exclude":
        where.append(f"NOT ({GONE_SQL})")
    elif gone == "only":
        where.append(GONE_SQL)
    if applied == "exclude":
        where.append(f"NOT ({APPLIED_FIRM_SQL})")
    elif applied == "only":
        where.append(APPLIED_FIRM_SQL)
    # The threshold is BOUND, and the fragment is appended together with its
    # value: the params list is positional, so a clause added without its
    # binding here would silently shift every later one.
    if old == "exclude":
        where.append(f"NOT {freshness.OLD_SQL}")
        params.append(stale_age_days)
    elif old == "only":
        where.append(freshness.OLD_SQL)
        params.append(stale_age_days)
    # Set aside by hand. Unlike the four piles above this is not a fact about
    # the posting but a decision of his, so it never hides anything by itself —
    # 'only' is the whole point, 'exclude' exists just so the vocabulary is the
    # same three words everywhere.
    if bookmarked == "exclude":
        where.append("bookmarked_at=''")
    elif bookmarked == "only":
        where.append("bookmarked_at<>''")
    # 'only' here means "not yet opened" — the unread half of an inbox. Named
    # after the column rather than after the view so the three words keep
    # meaning the same thing: 'only' is always "just the rows the column is
    # true of", and it is the caller that decides which half it wants.
    if opened == "exclude":
        # `keep_ids` is what makes an inbox usable: reading a posting in the
        # "Neu" view would otherwise drop it out of the list under his cursor
        # the moment the next tick ran, taking the reading pane with it. The
        # rows he opened during this sitting stay where they are; they are gone
        # the next time he opens the view, which is when he expects it.
        if keep_ids:
            places = ",".join("?" * len(keep_ids))
            where.append(f"(opened_at='' OR jobs.id IN ({places}))")
            params.extend(keep_ids)
        else:
            where.append("opened_at=''")
    elif opened == "only":
        where.append("opened_at<>''")
    # A plain substring over the two fields a person searches by. SQLite's LIKE
    # folds ASCII case only, which is what a search box needs; folding through
    # jd_norm would call a Python callback once per row for a query he retypes
    # on every keystroke.
    term = str(search or "").strip()
    if term:
        # ESCAPE, because % and _ are wildcards and he types them: searching
        # "100%" without this returns every posting containing "100", and a
        # single "_" returns the whole corpus. The guard tests the STRIPPED
        # value too — a query of spaces used to append "%%" and filter nothing.
        pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(jobs.title LIKE ? ESCAPE '\\' "
                     "OR jobs.company LIKE ? ESCAPE '\\')")
        params.extend((pattern, pattern))
    if in_progress == "exclude":
        where.append(f"NOT {_IN_PROGRESS_SQL}")
        params.extend(OPEN_DRAFT_STATUSES)
    elif in_progress == "only":
        where.append(_IN_PROGRESS_SQL)
        params.extend(OPEN_DRAFT_STATUSES)
    return where, params


# Postings are grouped by company because `find_duplicate_bewerbung` allows
# exactly ONE application per company: 36 companies held 83 of his 237 no-email
# postings, so 47 of those rows could never become an application and were only
# taking up places. An empty company name groups with nothing (its own id is the
# key) — a blank field is missing data, not a company they share.
# `jd_norm` is dedupe.norm itself (registered in connect()), so a grouped row's
# claim "one application per company" is judged by the very function that
# enforces it. The two branches are namespaced so a company literally called
# "job:7" cannot land in a blank row's group.
_COMPANY_KEY_SQL = (
    "CASE WHEN jd_norm(company)='' THEN 'job:'||id "
    "ELSE 'firma:'||jd_norm(company) END"
)
_JOB_ORDER_SQL = "effective_score DESC NULLS LAST, published_on DESC, id DESC"


def _ranked_jobs_cte(where_sql: str) -> str:
    """`ranked`: the filtered postings, each with its age-adjusted score, its
    company key, its rank inside that company and how many that company holds.
    One definition shared by the group list, its count and its siblings — three
    hand-written copies of this ranking would disagree about which posting
    represents a company."""
    return (
        "WITH filtered AS ("
        f" SELECT *, {freshness.AGE_SQL} AS age_days,"
        f" {freshness.effective_score_sql()} AS effective_score,"
        f" {_COMPANY_KEY_SQL} AS company_key,"
        f" {_DRAFT_STATUS_SQL} AS draft_status,"
        f" {_DRAFT_UPDATED_SQL} AS draft_updated_at,"
        f" {_DRAFT_PDF_SQL} AS pdf_path"
        f" FROM jobs{where_sql}"
        "), ranked AS ("
        " SELECT *, ROW_NUMBER() OVER ranking AS rank_in_company,"
        # COUNT over the RANKING window would be a running total: a window with
        # an ORDER BY frames rows up to the current one, so the best-ranked row
        # of every company would report a count of 1. The count needs a window
        # with no ordering, which frames the whole partition.
        " COUNT(*) OVER company AS company_count"
        " FROM filtered"
        f" WINDOW ranking AS (PARTITION BY company_key ORDER BY {_JOB_ORDER_SQL}),"
        " company AS (PARTITION BY company_key)"
        ") "
    )


def list_job_groups(
    con: sqlite3.Connection,
    status: str | None = None,
    limit: int = 500,
    mismatches: str = "include",
    gone: str = "include",
    applied: str = "include",
    old: str = "include",
    stale_age_days: int = freshness.DEFAULT_STALE_AGE_DAYS,
    bookmarked: str = "include",
    opened: str = "include",
    in_progress: str = "include",
    search: str = "",
    keep_ids: tuple[int, ...] = (),
    offset: int = 0,
) -> list[sqlite3.Row]:
    """One row per company: its best-ranked posting, plus `company_count`.

    Companies are ordered exactly as `list_jobs` orders postings — including
    the "all statuses" view's id ordering, so switching the grouping toggle
    never silently reorders the page under the user. Only the ranking WITHIN a
    company is always by score, because something has to choose which posting
    represents it."""
    where, params = _job_filters(status, mismatches, gone, applied, old,
                                 stale_age_days, bookmarked, opened,
                                 in_progress, search, keep_ids)
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    order = _JOB_ORDER_SQL if status else "id DESC"
    return con.execute(
        f"{_ranked_jobs_cte(where_sql)}"
        f"SELECT * FROM ranked WHERE rank_in_company=1 "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()


def count_job_groups(
    con: sqlite3.Connection,
    status: str | None = None,
    mismatches: str = "include",
    gone: str = "include",
    applied: str = "include",
    old: str = "include",
    stale_age_days: int = freshness.DEFAULT_STALE_AGE_DAYS,
    bookmarked: str = "include",
    opened: str = "include",
    in_progress: str = "include",
    search: str = "",
    keep_ids: tuple[int, ...] = (),
) -> int:
    """How many companies the grouped view holds."""
    where, params = _job_filters(status, mismatches, gone, applied, old,
                                 stale_age_days, bookmarked, opened,
                                 in_progress, search, keep_ids)
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    return con.execute(
        f"{_ranked_jobs_cte(where_sql)}"
        "SELECT COUNT(*) FROM ranked WHERE rank_in_company=1",
        params,
    ).fetchone()[0]


SIBLINGS_PER_COMPANY = 10


def list_company_siblings(
    con: sqlite3.Connection,
    company_keys: list[str],
    status: str | None = None,
    mismatches: str = "include",
    gone: str = "include",
    applied: str = "include",
    old: str = "include",
    stale_age_days: int = freshness.DEFAULT_STALE_AGE_DAYS,
    bookmarked: str = "include",
    opened: str = "include",
    in_progress: str = "include",
    search: str = "",
    keep_ids: tuple[int, ...] = (),
    per_company: int = SIBLINGS_PER_COMPANY,
) -> list[sqlite3.Row]:
    """The postings a grouped row stands in front of, best-ranked first.

    Asked only for the companies on the current page, and capped per company:
    one employer posting fifty near-identical roles must not decide how much a
    page renders. The caller has `company_count` to say how many there really
    are."""
    if not company_keys:
        return []
    where, params = _job_filters(status, mismatches, gone, applied, old,
                                 stale_age_days, bookmarked, opened,
                                 in_progress, search, keep_ids)
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    placeholders = ",".join("?" * len(company_keys))
    return con.execute(
        f"{_ranked_jobs_cte(where_sql)}"
        f"SELECT * FROM ranked WHERE rank_in_company>1 "
        f"AND rank_in_company<=? "
        f"AND company_key IN ({placeholders}) "
        f"ORDER BY company_key, rank_in_company",
        (*params, per_company + 1, *company_keys),
    ).fetchall()


def count_jobs(
    con: sqlite3.Connection,
    status: str | None = None,
    mismatches: str = "include",
    gone: str = "include",
    applied: str = "include",
    old: str = "include",
    stale_age_days: int = freshness.DEFAULT_STALE_AGE_DAYS,
    bookmarked: str = "include",
    opened: str = "include",
    in_progress: str = "include",
    search: str = "",
    keep_ids: tuple[int, ...] = (),
) -> int:
    """How many postings a `list_jobs` call with the same filters would have,
    ignoring its page limit — the total a paged view has to print."""
    where, params = _job_filters(status, mismatches, gone, applied, old,
                                 stale_age_days, bookmarked, opened,
                                 in_progress, search, keep_ids)
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    return con.execute(
        f"SELECT COUNT(*) FROM jobs{where_sql}", params
    ).fetchone()[0]


def list_jobs(
    con: sqlite3.Connection,
    status: str | None = None,
    limit: int = 500,
    mismatches: str = "include",
    gone: str = "include",
    applied: str = "include",
    old: str = "include",
    stale_age_days: int = freshness.DEFAULT_STALE_AGE_DAYS,
    bookmarked: str = "include",
    opened: str = "include",
    in_progress: str = "include",
    search: str = "",
    keep_ids: tuple[int, ...] = (),
    offset: int = 0,
) -> list[sqlite3.Row]:
    """List postings. mismatches: 'include' (default), 'exclude' (hide the
    score-0 rows, NULL-safe so unscored postings stay visible) or 'only'
    (just the hidden pile — keeps mismatches reachable regardless of how
    many better-scored rows fill the page limit). `gone` takes the same three
    values over postings whose ad the source says is no longer there."""
    where, params = _job_filters(status, mismatches, gone, applied, old,
                                 stale_age_days, bookmarked, opened,
                                 in_progress, search, keep_ids)
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    # The age-adjusted score is SELECTED as well as ordered on, so the number
    # the UI prints is the very number that decided the row's position — two
    # copies of that rule would drift (see freshness.py).
    derived = (f"{freshness.AGE_SQL} AS age_days, "
               f"{freshness.effective_score_sql()} AS effective_score, "
               f"{_DRAFT_STATUS_SQL} AS draft_status, "
               f"{_DRAFT_UPDATED_SQL} AS draft_updated_at, "
               f"{_DRAFT_PDF_SQL} AS pdf_path")
    order = _JOB_ORDER_SQL if status else "id DESC"
    return con.execute(
        f"SELECT *, {derived} FROM jobs{where_sql} "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, limit, offset),
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


def set_bookmark(con: sqlite3.Connection, job_id: int, marked: bool) -> bool:
    """Set a posting aside, or take the mark off again. Returns the new state.

    Independent of `status`: setting one aside is not acting on it. The
    timestamp is only rewritten when the mark is newly set, so re-marking an
    already-marked posting cannot move it to the top of the pile it is already
    in."""
    if marked:
        con.execute(
            "UPDATE jobs SET bookmarked_at=? WHERE id=? AND bookmarked_at=''",
            (_now(), job_id),
        )
    else:
        con.execute("UPDATE jobs SET bookmarked_at='' WHERE id=?", (job_id,))
    return marked


def mark_job_opened(con: sqlite3.Connection, job_id: int) -> None:
    """Record that he has now read this posting.

    Written once and never rewritten: the question the list asks is "have I
    looked at this yet", not "when did I last look", and re-stamping it on
    every visit would turn a stable order into one that moves while he reads
    down it."""
    con.execute(
        "UPDATE jobs SET opened_at=? WHERE id=? AND opened_at=''",
        (_now(), job_id),
    )


# A form application that is under way: he opened the employer's page and the
# loop is not closed. 'applied' and 'duplicate' are excluded because BOTH are
# written by `apply_job` — without the second the entry would vanish the
# instant the duplicate gate refused, which is indistinguishable from the
# eviction the strip exists to prevent. 'skipped' can only be reached by
# putting the posting away deliberately, which is an answer too.
_STARTED_FORM_SQL = (
    "jobs.form_opened_at<>'' "
    "AND jobs.status NOT IN ('applied','duplicate','skipped')"
)


def list_started_forms(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every form application under way, oldest first.

    Oldest first because the list is a stack of unfinished business, not a
    feed: the eleven that were open when this shipped surface top-down, and
    the one he has been ignoring longest is the one worth asking about.

    Nothing bounds this and nothing expires. An entry that aged out would take
    with it the app's only record that an application may already be at that
    company — and a company whose slot is spent silently is the one failure
    this whole design refuses.
    """
    return con.execute(
        f"SELECT jobs.*, {_DRAFT_STATUS_SQL} AS draft_status, "
        f"{_DRAFT_PDF_SQL} AS pdf_path "
        f"  FROM jobs WHERE {_STARTED_FORM_SQL} "
        # The sentinel first, not last. It sorts above every ISO timestamp only
        # by accident of collation, so it is ordered explicitly: a form opened
        # before the app could stamp it is the OLDEST thing here by
        # construction — it predates the release — and the strip is read
        # top-down.
        f" ORDER BY (form_opened_at <> ?) ASC, form_opened_at ASC, id ASC",
        (FORM_OPENED_UNKNOWN,),
    ).fetchall()


def count_started_forms(con: sqlite3.Connection) -> int:
    return con.execute(
        f"SELECT COUNT(*) FROM jobs WHERE {_STARTED_FORM_SQL}"
    ).fetchone()[0]


def mark_form_opened(con: sqlite3.Connection, job_id: int) -> None:
    """Record that he has opened this employer's form.

    Written once and never rewritten, like `opened_at` and for a stronger
    reason: this timestamp is how old a running application looks on the
    "Läuft" strip, and re-stamping it on a second press would make an
    application he began yesterday claim to have started just now."""
    con.execute(
        "UPDATE jobs SET form_opened_at=? WHERE id=? AND form_opened_at=''",
        (_now(), job_id),
    )


def clear_form_opened(con: sqlite3.Connection, job_id: int) -> None:
    """Take back "I started applying here" — he says no application went out.

    Blanks the staging columns too, but it cannot remove the FILE: this layer
    has no business touching the filesystem. `services.apply_record.
    abandon_form` is the way in, and it unlinks first — afterwards the pointer
    is gone and nothing could find that file again."""
    con.execute(
        "UPDATE jobs SET form_opened_at='', upload_path='', mappe_kind='' "
        "WHERE id=?",
        (job_id,),
    )


def set_upload(
    con: sqlite3.Connection, job_id: int, path: str, kind: str
) -> None:
    """Record what the build staged for an employer's file picker.

    `kind` is written BY the build and never inferred back out of the file
    system — the Unterlagen lesson. Empty means nothing complete is staged,
    which is a statement the screen has to make rather than a gap it fills in
    optimistically: a Bewerbungsmappe is always complete, so a partial one
    offered silently to an upload button is the worst outcome available."""
    con.execute(
        "UPDATE jobs SET upload_path=?, mappe_kind=? WHERE id=?",
        (path, kind, job_id),
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


def reset_job_scores(con: sqlite3.Connection, job_ids: list[int]) -> int:
    """Clear the match score of specific postings so the next batch re-scores
    them. Returns how many rows were cleared.

    Scores are immutable by design — a posting is paid for once. This is the
    deliberate exception: when the match CRITERIA change, the old verdict was
    answering a different question and has to be re-asked. Only postings that
    are still 'new' qualify; a scored-and-acted-on posting keeps its history."""
    if not job_ids:
        return 0
    placeholders = ",".join("?" * len(job_ids))  # ids bound, never interpolated
    cur = con.execute(
        f"UPDATE jobs SET match_score=NULL, match_reason='' "
        f"WHERE status='new' AND id IN ({placeholders})",
        job_ids,
    )
    return cur.rowcount


def set_apply_channel(
    con: sqlite3.Connection, job_id: int, channel: str, vendor: str, apply_url: str
) -> None:
    """Record where/how one applies to a posting (from the apply-channel
    classifier). Additive metadata — never touches status or the send path."""
    con.execute(
        "UPDATE jobs SET apply_channel=?, ats_vendor=?, apply_url=? WHERE id=?",
        (channel, vendor, apply_url, job_id),
    )


# Facts a source states about a posting, in this table's vocabulary. The
# writer accepts exactly these, so a source handing over a key nobody stores
# fails loudly here instead of being dropped in silence.
JOB_FACT_COLUMNS = ("work_strasse", "work_plz_ort", "salary_from", "salary_to",
                    "salary_period", "temp_agency")


def set_job_facts(con: sqlite3.Connection, job_id: int, facts: dict) -> int:
    """Store what a source stated about a posting. Returns the column count.

    Only values the source actually stated are written: a payload that omits a
    field must never erase what an earlier one said, and the same function
    therefore serves discovery and the daily liveness probe — which is how the
    postings stored before these columns existed fill in without one extra
    request. Additive metadata only: never status, never a draft."""
    known = {key: value for key, value in (facts or {}).items()
             if key in JOB_FACT_COLUMNS and value not in ("", None)}
    unknown = set(facts or {}) - set(JOB_FACT_COLUMNS)
    if unknown:
        raise ValueError(f"unknown job facts: {sorted(unknown)}")
    if not known:
        return 0
    assignments = ", ".join(f"{key}=?" for key in known)
    con.execute(f"UPDATE jobs SET {assignments} WHERE id=?",
                (*known.values(), job_id))
    return len(known)


def set_job_liveness(
    con: sqlite3.Connection, job_id: int, liveness: str | None
) -> None:
    """Record what a liveness probe observed. `None` means the server gave no
    answer: the attempt is timestamped so the pass rotates on, but the last
    real observation is kept — an unreachable host must not erase it.

    Additive metadata only: never touches status, a draft or an application."""
    if liveness is None:
        con.execute(
            "UPDATE jobs SET liveness_checked_at=? WHERE id=?", (_now(), job_id)
        )
        return
    con.execute(
        "UPDATE jobs SET liveness=?, liveness_checked_at=? WHERE id=?",
        (liveness, _now(), job_id),
    )


def refresh_job_published_on(
    con: sqlite3.Connection, job_id: int, raw: str
) -> bool:
    """Correct a posting's DERIVED publication date from a fresher statement by
    the source, returning whether anything changed.

    Only `published_on` moves. The raw `published_at` stays as the search
    payload sent it, so the backfill's "fill blanks only" rule still holds and
    the two values can always be compared. Used by the liveness pass, where the
    answer that proves an ad is alive also says when its current version went
    up — an ad re-published last week is fresh however long ago it first
    appeared."""
    iso = dates.posting_date_iso(raw)
    if not iso:
        return False
    cur = con.execute(
        "UPDATE jobs SET published_on=? WHERE id=? AND published_on<>?",
        (iso, job_id, iso),
    )
    return cur.rowcount > 0


# A posting is worth asking about while he might still act on it. 'skipped',
# 'duplicate' and 'applied' are finished business.
#
# A posting whose form he has opened stays 'new' since v10, so it keeps being
# probed through this list — which is the point, and it is the OPPOSITE of
# ruled out: a half-finished form application is precisely when "the ad is
# gone" is worth five minutes of his time.
LIVENESS_STATUSES = ("new", "drafted")


def jobs_needing_liveness_check(
    con: sqlite3.Connection,
    limit: int,
    sources: tuple[str, ...],
    recheck_after_h: int,
    recheck_gone_after_h: int = 168,
    min_score: int = 1,
    statuses: tuple[str, ...] = LIVENESS_STATUSES,
) -> list[sqlite3.Row]:
    """Postings worth asking about, longest-unchecked first.

    Restricted to sources that can be asked at all (Jooble's URLs are all
    robots-disallowed, so probing it is not an option) and to postings he may
    still act on, above the mismatch floor — resolving the fate of a posting he
    ruled out is work nobody asked for.

    A posting already seen `gone` is re-asked far more rarely rather than never:
    that keeps the daily pass cheap while letting a systematic wrong answer (a
    board answering 404 to everything for an hour) heal itself instead of
    hiding real postings forever.
    """
    if not sources or not statuses:
        return []
    placeholders = ",".join("?" * len(sources))
    status_places = ",".join("?" * len(statuses))
    cutoff = _hours_ago(recheck_after_h)
    gone_cutoff = _hours_ago(recheck_gone_after_h)
    return con.execute(
        f"""
        SELECT * FROM jobs
         WHERE status IN ({status_places})
           AND source IN ({placeholders})
           AND (match_score IS NULL OR match_score>=?)
           AND (liveness_checked_at=''
                OR ({GONE_SQL} AND liveness_checked_at<?)
                OR (NOT ({GONE_SQL}) AND liveness_checked_at<?))
         ORDER BY liveness_checked_at ASC,
                  match_score DESC NULLS LAST, id DESC
         LIMIT ?
        """,
        (*statuses, *sources, min_score, gone_cutoff, cutoff, limit),
    ).fetchall()


# Draft states that mean "this posting already has an application in progress",
# so the daily batch must not draft it a second time. `failed` is deliberately
# absent: a posting whose drafting broke is a fair candidate again. `filed` is
# present for the same reason as `sent` — the letter is with the employer, and
# paying to write a second one would be paying to duplicate an application.
PREPARED_DRAFT_STATUS = ("generating", "ready", "approved", "sending", "sent",
                         "filed")


def jobs_to_prepare(
    con: sqlite3.Connection,
    limit: int,
    max_age_days: int,
    min_score: int,
    include_forms: bool = True,
) -> list[sqlite3.Row]:
    """The postings worth writing an application for, best-ranked first.

    The filters are the ones he asked for — no older than `max_age_days`, at
    least `min_score` — plus the three the rest of the app already enforces and
    that would otherwise waste a paid draft:

    * a posting whose ad is gone, or that violates a hard requirement (score 0),
      is never a candidate;
    * a company he has ALREADY applied to is skipped, because
      `find_duplicate_bewerbung` would refuse the second application anyway —
      compared with `jd_norm`, the very function that gate uses;
    * a posting that already has a draft in flight is skipped;
    * a posting whose form he has already opened is skipped. Before v10 that
      one fell out of `status='new'` for free, because opening a form moved the
      posting to `portal`; now that it stays in the working list it has to be
      excluded here by name, or a started application would be handed back to
      the batch and re-drafted at the price of a Sonnet call.

    An unknown publication date excludes a posting here, unlike in the inbox —
    and it falls out of the age bound rather than needing its own clause, since
    an unreadable date makes the age NULL and `NULL <= n` is never true. The
    inbox must not hide a posting for missing information; a paid draft should
    go to one we can vouch is current.
    """
    placeholders = ",".join("?" * len(PREPARED_DRAFT_STATUS))
    return con.execute(
        f"""
        SELECT j.*, {freshness.AGE_SQL} AS age_days,
               {freshness.effective_score_sql()} AS effective_score
          FROM jobs j
         WHERE j.status='new'
           AND j.form_opened_at=''
           AND NOT ({GONE_SQL.replace('liveness', 'j.liveness')})
           AND j.duplicate_of IS NULL
           AND j.match_score >= ?
           AND {freshness.AGE_SQL} <= ?
           AND (? OR j.contact_email <> '')
           AND NOT EXISTS (
                 SELECT 1 FROM drafts d
                  WHERE d.job_id = j.id AND d.status IN ({placeholders}))
           AND NOT ({APPLIED_FIRM_SQL.replace("jobs.", "j.")})
         ORDER BY effective_score DESC, j.published_on DESC, j.id DESC
         LIMIT ?
        """,
        (min_score, max_age_days, 1 if include_forms else 0,
         *PREPARED_DRAFT_STATUS, limit),
    ).fetchall()


def count_waiting_drafts(con: sqlite3.Connection) -> int:
    """Applications written and still waiting to be sent.

    This is the meter for "prepare N a day", not the number of drafts CREATED
    today: what he asked to see is a queue holding N applications, so a draft he
    discarded must free its place, and one he sent must free it too. Both
    thresholds still bind — a single press never prepares more than N."""
    return con.execute(
        "SELECT COUNT(*) FROM drafts WHERE status IN ('ready','approved')"
    ).fetchone()[0]


def count_open_drafts(con: sqlite3.Connection) -> int:
    """Everything the Postausgang's own "Warten" tab lists.

    Wider than `count_waiting_drafts` on purpose, and for a different reader:
    that one is the "prepare N a day" quota and counts only letters that could
    be SENT, while this one is what the rail's shelf promises to open. With
    the narrower figure a register holding one failed draft and one stuck send
    drew no shelf at all, while the tab behind it held two rows each waiting
    on a decision — and the shelf is that tab's only entry point.
    """
    places = ",".join("?" * len(OPEN_DRAFT_STATUSES))
    return con.execute(
        f"SELECT COUNT(*) FROM drafts WHERE status IN ({places})",
        OPEN_DRAFT_STATUSES,
    ).fetchone()[0]


def pipeline_counts(con: sqlite3.Connection) -> dict:
    """Every population the Bewerbungen screen measures, in one statement.

    One SELECT rather than six, because they are shown side by side and read
    against each other: taken separately, sqlite3 gives each its own snapshot,
    so a poll committing between two of them can put a posting in the later
    number and not the earlier one — and "more letters than postings" is
    exactly the kind of impossible pair a reader stops trusting the screen for.

    `drafted_unread` is the one that has to be measured rather than inferred:
    the daily batch and the form flow both write a letter without the posting
    ever being opened, so the column is not a chain of subsets and the screen
    has to say where it breaks.
    """
    row = con.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM jobs) AS jobs_total,
          (SELECT COUNT(*) FROM jobs WHERE match_score > 0) AS scored_above_zero,
          (SELECT COUNT(*) FROM jobs WHERE match_score = 0) AS scored_zero,
          (SELECT COUNT(*) FROM jobs WHERE opened_at <> '') AS opened,
          -- a letter EXISTS, not a draft row exists: 'generating' has an
          -- empty body until the model answers and 'failed' never got one at
          -- all, so counting rows would print them under "Anschreiben
          -- geschrieben". A discarded letter was written and stays counted.
          (SELECT COUNT(DISTINCT job_id) FROM drafts
            WHERE anschreiben_body <> '') AS drafted,
          (SELECT COUNT(DISTINCT d.job_id) FROM drafts d
             JOIN jobs j ON j.id = d.job_id
            WHERE d.anschreiben_body <> '' AND j.opened_at = '') AS drafted_unread,
          (SELECT COUNT(*) FROM jobs WHERE bewerbung_id IS NOT NULL) AS applied,
          -- MEASURED, not subtracted: `applied` and `drafted` are different
          -- sets, so `applied - drafted` is only a lower bound and reads as
          -- zero whenever more letters exist than applications, however many
          -- of those applications carried none.
          (SELECT COUNT(*) FROM jobs j
            WHERE j.bewerbung_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM drafts d
                               WHERE d.job_id = j.id
                                 AND d.anschreiben_body <> ''))
            AS applied_without_letter
        """
    ).fetchone()
    return dict(row)


def applications_by_source(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Per board: how many postings it delivered, and how many became one.

    Counted off `jobs`, so only the applications this app recorded appear —
    an imported row carries no posting and therefore no board, and folding
    those into a per-source figure would credit a board with work it never did.
    """
    return con.execute(
        """
        SELECT source,
               COUNT(*) AS jobs,
               SUM(CASE WHEN bewerbung_id IS NOT NULL THEN 1 ELSE 0 END) AS applied
          FROM jobs
         GROUP BY source
        """
    ).fetchall()


def count_drafts_created_today(con: sqlite3.Connection) -> int:
    """Drafts written since local midnight — reported so the cost of the day is
    visible, never used as the quota (see count_waiting_drafts)."""
    today = datetime.date.today().isoformat()
    return con.execute(
        "SELECT COUNT(*) FROM drafts WHERE created_at LIKE ?", (f"{today}%",)
    ).fetchone()[0]


def count_gone_jobs(con: sqlite3.Connection, status: str | None = None) -> int:
    """How many postings the liveness filter would hide for this inbox view."""
    sql = f"SELECT COUNT(*) FROM jobs WHERE {GONE_SQL}"
    params: tuple = ()
    if status:
        sql += " AND status=?"
        params = (status,)
    return con.execute(sql, params).fetchone()[0]


def count_applied_firm_jobs(con: sqlite3.Connection, status: str | None = None) -> int:
    """How many postings the already-applied filter would hide for this view."""
    sql = f"SELECT COUNT(*) FROM jobs WHERE {APPLIED_FIRM_SQL}"
    params: tuple = ()
    if status:
        sql += " AND status=?"
        params = (status,)
    return con.execute(sql, params).fetchone()[0]


def count_old_jobs(
    con: sqlite3.Connection, status: str | None = None,
    stale_age_days: int = freshness.DEFAULT_STALE_AGE_DAYS,
) -> int:
    """How many postings the age filter would hide for this inbox view."""
    sql = f"SELECT COUNT(*) FROM jobs WHERE {freshness.OLD_SQL}"
    params: list = [stale_age_days]
    if status:
        sql += " AND status=?"
        params.append(status)
    return con.execute(sql, params).fetchone()[0]


def count_bookmarked_jobs(con: sqlite3.Connection) -> int:
    """How many postings he has set aside, across every status.

    Deliberately not narrowed to one status: the mark survives applying and
    skipping, and a count that quietly dropped those would disagree with the
    view it labels."""
    return con.execute(
        "SELECT COUNT(*) FROM jobs WHERE bookmarked_at<>''"
    ).fetchone()[0]


def jobs_needing_apply_channel(
    con: sqlite3.Connection, limit: int, min_score: int = 1
) -> list[sqlite3.Row]:
    """Postings still worth acting on whose apply channel is unresolved.

    Best-scored first, because the resolve pass is bounded and the postings he
    will actually open are the ones at the top. Score 0 is excluded: it means a
    hard requirement is violated, and resolving where to apply to a job he
    ruled out is work nobody asked for.
    """
    return con.execute(
        "SELECT * FROM jobs WHERE status='new' AND COALESCE(apply_channel,'')='' "
        "AND match_score IS NOT NULL AND match_score >= ? "
        "ORDER BY match_score DESC, id LIMIT ?",
        (min_score, limit),
    ).fetchall()


def count_jobs_needing_apply_channel(
    con: sqlite3.Connection, min_score: int = 1
) -> int:
    """How many postings are still waiting — the TRUE total, not one page of
    them. A bounded fetch would answer at most `limit`, and telling the user
    "61 pending" when 219 are is worse than not telling them."""
    return con.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='new' "
        "AND COALESCE(apply_channel,'')='' "
        "AND match_score IS NOT NULL AND match_score >= ?",
        (min_score,),
    ).fetchone()[0]


def set_contact_email(
    con: sqlite3.Connection, job_id: int, email: str, source: str
) -> None:
    """Adopt a human-confirmed application e-mail for a posting (from the
    web contact lookup). `source` records provenance (e.g. 'web_lookup'). The
    address becomes the send recipient only through the normal draft→queue path,
    still behind real_send_enabled and the per-send human confirmation.

    An e-mail arriving is also the moment the posting stops being a form job —
    so the channel is recomputed here rather than waiting for a resolve pass
    that may never be run again on this row."""
    con.execute(
        "UPDATE jobs SET contact_email=?, contact_source=? WHERE id=?",
        (email, source, job_id),
    )
    resolve_email_channels(con, job_id)


# Statuses whose channel is still worth deciding. A posting already applied to,
# skipped or filed as a duplicate is finished business — rewriting how one
# would have applied to it changes nothing and would only rewrite history.
# A posting at an open form is 'new' since v10 and stays in scope: its channel
# is what the strip's "open the form again" needs.
_CHANNEL_STATUSES = ("new",)


def resolve_email_channels(
    con: sqlite3.Connection, job_id: int | None = None
) -> int:
    """Record 'apply by e-mail' wherever a posting already holds one. Returns
    the number of postings that changed.

    A stored `contact_email` settles the question with no network at all: it is
    the first rule of `apply_channel.classify` and the short-circuit
    `apply_resolve.resolve` takes before it fetches anything. Nothing applied
    that rule outside the resolver, so 81 of his Arbeitsagentur postings held an
    application address and still read as unresolved — form jobs, in an app
    whose whole pain is form jobs — while only 5 said 'direct_email'.

    Only the CHANNEL is written: the ATS vendor and the resolved apply URL stay,
    because they remain true about the posting and the row still offers to open
    it.
    """
    statuses = ",".join("?" * len(_CHANNEL_STATUSES))
    where_id = " AND id=?" if job_id is not None else ""
    params = [apply_channel.CHANNEL_DIRECT_EMAIL, apply_channel.CHANNEL_DIRECT_EMAIL,
              *_CHANNEL_STATUSES]
    if job_id is not None:
        params.append(job_id)
    cur = con.execute(
        "UPDATE jobs SET apply_channel=? "
        "WHERE contact_email<>'' AND COALESCE(apply_channel,'')<>? "
        f"AND status IN ({statuses}){where_id}",
        params,
    )
    return cur.rowcount


def count_jobs_by_status(con: sqlite3.Connection) -> dict[str, int]:
    rows = con.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
    return {row["status"]: row["n"] for row in rows}


def count_active_profiles(con: sqlite3.Connection) -> int:
    """How many search profiles are switched on.

    Its own read because `profiles_signature` cannot see a profile being
    deactivated — it carries COUNT, MAX(id), the poll stamps and the errors,
    none of which move when `active` flips."""
    return con.execute(
        "SELECT COUNT(*) FROM search_profiles WHERE active=1"
    ).fetchone()[0]


def count_unscored_jobs(con: sqlite3.Connection) -> int:
    """New postings still waiting for a match score — the scoring backlog."""
    return con.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='new' AND match_score IS NULL"
    ).fetchone()[0]


def poll_progress(con: sqlite3.Connection) -> tuple[int, str, int]:
    """(active profiles, when one was last polled, how many last failed).

    What the rail says about discovery. The error COUNT rather than the text:
    the rail has one line, and "2 Quellen melden einen Fehler" sends him to the
    page that states them."""
    row = con.execute(
        "SELECT COUNT(*), MAX(COALESCE(last_polled_at,'')), "
        "TOTAL(COALESCE(last_poll_error,'')<>'') "
        "FROM search_profiles WHERE active=1"
    ).fetchone()
    return int(row[0]), str(row[1] or ""), int(row[2])


def liveness_progress(
    con: sqlite3.Connection, sources: tuple[str, ...], min_score: int = 1
) -> tuple[str, int]:
    """(when a posting was last probed, how many the pass has still to reach).

    Counted over exactly what `jobs_needing_liveness_check` would select — the
    askable sources, the statuses it looks at, AND the mismatch floor. Leaving
    the floor out counted every score-0 posting as pending, which the pass will
    never probe: a backlog that can never reach zero, pulsing on every screen
    forever."""
    if not sources:
        return "", 0
    row = con.execute(
        "SELECT MAX(COALESCE(liveness_checked_at,'')), "
        "TOTAL(COALESCE(liveness_checked_at,'')='') "
        f"FROM jobs WHERE source IN ({','.join('?' * len(sources))}) "
        f"AND status IN ({','.join('?' * len(LIVENESS_STATUSES))}) "
        "AND (match_score IS NULL OR match_score>=?)",
        (*sources, *LIVENESS_STATUSES, min_score),
    ).fetchone()
    return str(row[0] or ""), int(row[1])

# --------------------------------------------------------------------------
# Signatures — "has anything I display changed?" in one cheap query
# --------------------------------------------------------------------------
# A page polls one of these and rebuilds only when the value differs from what
# it is showing (see ui/live.py). DERIVED rather than a version counter that
# writers bump: a bump can be forgotten, and a forgotten bump is exactly the
# silent staleness this mechanism exists to end — every write to these tables
# moves one of the aggregates by construction.
#
# The jobs part is ONE scan with aggregates only; the per-status counts make a
# status change visible even though the row count does not move (a transition
# lowers one count and raises another, so only two opposite transitions inside
# a single tick could cancel out). `status_history` is included through its id
# alone because `set_status` writes an audit row for every application status
# change — the one thing about a bewerbung a job row quotes.
_JOBS_SIGNATURE_SQL = """
SELECT COUNT(*), MAX(id), COUNT(match_score), TOTAL(match_score),
       MAX(liveness_checked_at), TOTAL(liveness=?),
       TOTAL(status='new'), TOTAL(status='applied'),
       TOTAL(status='skipped'), TOTAL(status='duplicate'),
       TOTAL(contact_email<>''), TOTAL(COALESCE(apply_channel,'')<>''),
       TOTAL(bookmarked_at<>''), TOTAL(opened_at<>''),
       TOTAL(form_opened_at<>''), TOTAL(upload_path<>''), TOTAL(mappe_kind<>'')
  FROM jobs
"""

# Drafts get the same treatment for the same reason, and NOT a MAX(updated_at):
# the timestamp has second resolution, so two moves inside one second (a
# discard and a re-draft, a test that writes both) would compare equal while
# the queue shows something different.
# Derived from the vocabulary rather than written out: the hand-written list
# was missing 'filed' the moment that status existed, so two moves inside one
# second between a filed row and a ready one compared equal — exactly the case
# the per-status totals were added for. A new status now joins it by existing.
_DRAFTS_SIGNATURE_SQL = (
    "SELECT COUNT(*), MAX(id), MAX(updated_at), "
    + ", ".join(f"TOTAL(status='{status}')"
                for status in DRAFT_STATUS)
    + " FROM drafts"
)

_APPLICATIONS_SIGNATURE_SQL = """
SELECT (SELECT COUNT(*) FROM bewerbungen), (SELECT MAX(id) FROM bewerbungen),
       (SELECT MAX(id) FROM status_history)
"""

# Inbound mail joined the log in v12. COUNT/MAX see arrivals; the two totals
# see the transitions review actions make without adding rows (a confirm
# flips needs_review, a correction rewrites classification — the count of
# rows moves for neither).
_EMAIL_SIGNATURE_SQL = """
SELECT COUNT(*), MAX(id), TOTAL(needs_review),
       TOTAL(classification<>''), TOTAL(bewerbung_id IS NOT NULL)
  FROM email_log
"""


def data_signature(con: sqlite3.Connection) -> tuple:
    """What the pipeline pages (inbox, queue, dashboard, applications, cockpit)
    are showing, compressed to one comparable tuple.

    One signature for all of them on purpose: a page-specific one would have to
    predict which writes it cares about, and being rebuilt by an unrelated
    change costs a render nobody notices, while missing a related one is the
    defect."""
    return (
        *con.execute(_JOBS_SIGNATURE_SQL, (LIVENESS_GONE,)).fetchone(),
        *con.execute(_DRAFTS_SIGNATURE_SQL).fetchone(),
        *con.execute(_APPLICATIONS_SIGNATURE_SQL).fetchone(),
        *con.execute(_EMAIL_SIGNATURE_SQL).fetchone(),
    )


def job_signature(con: sqlite3.Connection, job_id: int) -> tuple | None:
    """One posting's state, for a page that stands beside ONE form.

    Per-posting rather than the whole pipeline: the apply cockpit sits open for
    many minutes while he types into an employer's form, and rebuilding it every
    time an unrelated posting is scored would move the buttons under his hand.
    `None` when the posting is gone.

    The contact block is signed as well as the channel. Both the cockpit and
    the letter preview PRINT the Ansprechpartner and the postal address, and
    contact resolution fills exactly those fields in the background — a screen
    that states a value has to be rebuilt when that value changes, or it is
    quietly showing "none named" beside a name the app already has.

    `id` and `company` lead it because a caller may CHOOSE which posting to
    sign — the letter preview signs whichever posting currently tops the
    working list. Without them, two unresolved postings sharing a scraped
    title produce identical tuples, so the preview goes on naming a firm whose
    posting has just been skipped, applied to or outranked.
    """
    row = con.execute(
        "SELECT id, company, status, liveness, liveness_checked_at, "
        "contact_email, apply_channel, ats_vendor, apply_url, title, "
        "ansprechpartner, contact_strasse, contact_plz_ort, work_strasse, "
        "work_plz_ort, temp_agency, refnr, "
        "form_opened_at, upload_path, mappe_kind, "
        f"{_DRAFT_STATUS_SQL}, "
        f"{_DRAFT_UPDATED_SQL} FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    return None if row is None else tuple(row)


def meter_signature(con: sqlite3.Connection) -> tuple:
    """The Settings numbers that move on their own: LLM spend and today's
    sends. Deliberately NOT the whole settings snapshot — the page must never
    overwrite an input he is typing in, so only the meters are polled."""
    return (
        get_setting(con, "llm_calls", "0"),
        get_setting(con, "llm_input_tokens", "0"),
        get_setting(con, "llm_output_tokens", "0"),
        get_setting(con, "llm_cost_usd", "0"),
        count_outbound_today(con),
    )


def profiles_signature(con: sqlite3.Connection) -> tuple:
    """What the search-profile list shows about the poller: when each profile
    was last polled and what it said. A handful of rows, so the errors are
    compared verbatim rather than through an aggregate that could hide one."""
    return tuple(con.execute(
        "SELECT COUNT(*), MAX(id), MAX(COALESCE(last_polled_at,'')), "
        "GROUP_CONCAT(COALESCE(last_poll_error,''), '|') "
        "FROM search_profiles"
    ).fetchone())


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


def claim_for_send(
    con: sqlite3.Connection, draft_id: int, test_mode: bool
) -> None:
    """Take the send claim, recording which mode it was taken in.

    sending_test is deliberately outside the upsert_draft allowlist: only
    the claim may set it, and only from the mode it resolved atomically."""
    con.execute(
        "UPDATE drafts SET status='sending', sending_test=?, updated_at=? "
        "WHERE id=?",
        (int(test_mode), _now(), draft_id),
    )


def file_draft(con: sqlite3.Connection, draft_id: int,
               bewerbung_id: int) -> None:
    """Mark a letter as delivered by hand, inside an uploaded Mappe.

    The form path's counterpart to `record_send`, and a dedicated writer for
    the same reason: 'filed' is a claim that an employer has this letter, so
    only the code that recorded the application may make it.

    Without it a letter stayed 'ready' for ever after its own application went
    out — twelve of the seventeen letters waiting in his queue were of that
    kind, each one offering to be e-mailed to a company he had applied to that
    same afternoon.
    """
    con.execute(
        "UPDATE drafts SET status='filed', bewerbung_id=?, error='',"
        " updated_at=? WHERE id=?",
        (bewerbung_id, _now(), draft_id),
    )


def unfile_draft(con: sqlite3.Connection, bewerbung_id: int) -> None:
    """Give a filed letter back when its application is taken back.

    Part of `unrecord_application`'s all-or-nothing undo: an undo that left
    the letter filed would leave him with no way to send it and no way to
    rewrite it — the shape that once made a posting undraftable for ever.
    """
    con.execute(
        "UPDATE drafts SET status='ready', bewerbung_id=NULL, updated_at=?"
        " WHERE bewerbung_id=? AND status='filed'",
        (_now(), bewerbung_id),
    )


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


_DRAFT_WITH_JOB_COLUMNS = """
        SELECT d.*, j.title AS job_title, j.company AS job_company,
               j.url AS job_url, j.match_score AS job_score,
               j.location AS job_location, j.status AS job_status,
               j.contact_email AS job_contact_email,
               j.liveness AS job_liveness,
               j.liveness_checked_at AS job_liveness_checked_at
        FROM drafts d JOIN jobs j ON j.id = d.job_id
"""


def draft_with_job(con: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    """One posting's newest draft, with the job fields the editor shows.

    `job_id` has no UNIQUE constraint on drafts — a posting can carry a
    discarded draft and a live one — so this picks the newest, exactly as
    `get_draft_by_job` does.
    """
    return con.execute(
        _DRAFT_WITH_JOB_COLUMNS + " WHERE d.job_id=? ORDER BY d.id DESC LIMIT 1",
        (job_id,),
    ).fetchone()


def send_mode(con: sqlite3.Connection) -> dict:
    """Whether a send would be real, and what today's budget has left.

    Shared because two screens now stand in front of the same send: the review
    queue's banner and the pre-send confirmation wherever it is opened from.
    """
    return {
        "real": get_setting(con, "real_send_enabled", "0") == "1",
        "test_recipient": get_setting(con, "test_recipient", "").strip(),
        "cap": get_setting(con, "daily_send_cap", DEFAULT_DAILY_CAP),
        "sent_today": count_outbound_today(con),
    }


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
               j.contact_email AS job_contact_email,
               -- the queue is the last place before a Bewerbung leaves: one
               -- draft (job 18) was written and a 2.1 MB Mappe built for an ad
               -- that had been gone forty days
               j.liveness AS job_liveness,
               j.liveness_checked_at AS job_liveness_checked_at
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
             matched_by, classification, classified_by, needs_review,
             body_text, job_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            values.get("body_text", ""),
            values.get("job_id"),
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


_DRAFT_DAY_KEY = "drafts_written_date"
_DRAFT_COUNT_KEY = "drafts_written_count"


def note_draft_written(con: sqlite3.Connection) -> None:
    """Count one paid letter against today's quota.

    Called where the money is committed — the claim — rather than where a
    letter comes back, because a drafting call that fails has still spent the
    tokens. The send cap counts test sends for the same reason.

    A counter of its own rather than a query over `drafts`: `updated_at` moves
    when the Mappe is written too, so counting rows would charge him for
    building a PDF.
    """
    today = datetime.date.today().isoformat()
    if get_setting(con, _DRAFT_DAY_KEY, "") != today:
        set_setting(con, _DRAFT_DAY_KEY, today)
        set_setting(con, _DRAFT_COUNT_KEY, "0")
    _bump_int_setting(con, _DRAFT_COUNT_KEY, 1)


def count_drafts_today(con: sqlite3.Connection) -> int:
    """Letters written since local midnight. Zero on a new day without any
    write having happened — the stored count belongs to the stored date."""
    if get_setting(con, _DRAFT_DAY_KEY, "") != datetime.date.today().isoformat():
        return 0
    try:
        return max(0, int(get_setting(con, _DRAFT_COUNT_KEY, "0") or 0))
    except (TypeError, ValueError):
        return 0


def daily_draft_cap(con: sqlite3.Connection) -> int:
    """How many letters may be written in a day. His decision, 2026-08-15: a
    HARD cap, raised deliberately in Einstellungen rather than by a one-press
    override, because an override always one press away is not a limit."""
    raw = (get_setting(con, "daily_draft_cap", "") or "").strip()
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError, OverflowError):
        # OverflowError as well: float("1e999") is inf and int(inf) raises —
        # app_settings is a file he is invited to edit
        return int(DEFAULT_DAILY_DRAFT_CAP)


def next_approved_autosend_job(
    con: sqlite3.Connection, exclude_test_sent: bool = False
) -> int | None:
    """Oldest approved draft whose search profile opted into auto-send.

    Requires the profile to be active too: deactivating a profile pauses
    everything about that search, including automatic transmission.

    exclude_test_sent skips drafts already rehearsed to the test inbox. A
    test send deliberately leaves the draft approved (it consumes nothing),
    so without this the worker would re-pick the same draft every window
    and burn the daily cap on one posting instead of draining the queue.
    Real sends need no such filter: they end at status 'sent'."""
    skip_rehearsed = (
        "AND NOT EXISTS (SELECT 1 FROM email_log e "
        "WHERE e.draft_id = d.id AND e.direction = ?) "
        if exclude_test_sent else ""
    )
    params = (EMAIL_OUTBOUND_TEST,) if exclude_test_sent else ()
    row = con.execute(
        f"""
        SELECT d.job_id FROM drafts d
        JOIN jobs j ON j.id = d.job_id
        JOIN search_profiles p ON p.id = j.profile_id
        WHERE d.status='approved' AND p.auto_send=1 AND p.active=1
        {skip_rehearsed}
        ORDER BY d.id LIMIT 1
        """,
        params,
    ).fetchone()
    return row["job_id"] if row is not None else None


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


def unrecord_application(
    con: sqlite3.Connection,
    job_id: int,
    bewerbung_id: int,
    previous_status: str,
) -> None:
    """Undo `apply_job` completely — every write it made, in one transaction.

    This exists so a form application can be recorded with a press and taken
    back with a press, instead of being guarded by a confirmation dialog he
    would learn to click through. That trade is only honest if the undo is
    REAL: a half-undo leaves a company marked as applied-to and permanently
    spends its only application slot, silently.

    Four writes, because `apply_job` made four: `add_bewerbung` writes a
    `status_history` row of its own, and `set_job_status` sets both the status
    and the link. `previous_status` is what the posting was BEFORE — restoring
    a hardcoded 'new' would quietly revive a posting he had skipped.
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        # The posting first: `jobs.bewerbung_id` is a foreign key into the row
        # about to be deleted, so deleting it while the link stands raises.
        con.execute(
            "UPDATE jobs SET status=?, bewerbung_id=NULL WHERE id=?",
            (previous_status, job_id),
        )
        # `jobs.duplicate_of` is a SECOND foreign key into the row being
        # deleted, and it is written by the very gate this application armed:
        # every posting refused because of it points here. Leaving them would
        # make the DELETE raise — inside a worker thread, so one log line, a
        # bar that vanishes and a user who believes the undo happened — and
        # they are not duplicates of an application that never existed.
        con.execute(
            "UPDATE jobs SET status='new', duplicate_of=NULL "
            " WHERE duplicate_of=? AND bewerbung_id IS NULL",
            (bewerbung_id,),
        )
        # `email_log.bewerbung_id` is a FOURTH foreign key into the row about
        # to be deleted. `delete_bewerbung` clears it and this path did not, so
        # an undo after any e-mail had been logged against the application
        # raised — inside a worker thread, so a log line and a bar that simply
        # vanishes, and a user who believes the undo happened.
        con.execute(
            "UPDATE email_log SET bewerbung_id=NULL WHERE bewerbung_id=?",
            (bewerbung_id,),
        )
        con.execute("DELETE FROM status_history WHERE bewerbung_id=?",
                    (bewerbung_id,))
        # `drafts.bewerbung_id` is a THIRD foreign key into the row being
        # deleted, written by the filing that recording just did. Leaving it
        # would make the DELETE raise — in a worker thread, so a log line and
        # a bar that simply vanishes — and would strand the letter in a state
        # nothing can send and nothing can rewrite.
        unfile_draft(con, bewerbung_id)
        con.execute("DELETE FROM bewerbungen WHERE id=?", (bewerbung_id,))
    except Exception:
        con.rollback()
        raise
    con.commit()


# --------------------------------------------------------------------------
# Inbound replies (Phase 3)
# --------------------------------------------------------------------------
def known_gmail_ids(con: sqlite3.Connection, ids: list[str]) -> set[str]:
    """Which of these Gmail message ids the log already holds.

    The ingestion pass asks this before fetching anything: the UNIQUE column
    is what makes an overlapping re-list idempotent, and asking first is what
    makes it cheap."""
    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    rows = con.execute(
        f"SELECT gmail_message_id FROM email_log "
        f" WHERE gmail_message_id IN ({placeholders})",
        ids,
    ).fetchall()
    return {row[0] for row in rows}


def find_bewerbung_by_thread(con: sqlite3.Connection, thread_id: str) -> int | None:
    """The application a Gmail thread belongs to, if this app sent into it.

    Only rows that carry a `bewerbung_id` count: a TEST send shares no thread
    with any company, and its row has none — so rehearsal traffic can never
    match a reply to an application."""
    if not thread_id:
        return None
    row = con.execute(
        "SELECT bewerbung_id FROM email_log "
        " WHERE gmail_thread_id=? AND direction LIKE ? "
        "   AND bewerbung_id IS NOT NULL "
        " ORDER BY id DESC LIMIT 1",
        (thread_id, f"{EMAIL_OUTBOUND}%"),
    ).fetchone()
    if row is not None:
        return int(row[0])
    row = con.execute(
        "SELECT bewerbung_id FROM drafts "
        " WHERE gmail_thread_id=? AND bewerbung_id IS NOT NULL "
        " ORDER BY id DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    return int(row[0]) if row is not None else None


def bewerbungen_for_reply_match(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every application that could receive mail: id, address, status, firma.

    The address arms of the match cascade compare against these; rows with no
    stored address (form applications at portals) simply cannot be matched
    that way and are excluded here rather than skipped by every caller."""
    return con.execute(
        "SELECT id, email, firma, status, gesendet_am FROM bewerbungen "
        " WHERE COALESCE(email,'') <> '' ORDER BY id DESC"
    ).fetchall()


def bewerbung_has_inbound(con: sqlite3.Connection, bewerbung_id: int) -> bool:
    row = con.execute(
        "SELECT 1 FROM email_log WHERE bewerbung_id=? AND direction=? LIMIT 1",
        (bewerbung_id, EMAIL_INBOUND),
    ).fetchone()
    return row is not None


def receipt_candidates(con: sqlite3.Connection, cutoff: str) -> list[sqlite3.Row]:
    """Postings an Eingangsbestätigung could belong to: the Läuft strip.

    One to three rows, never the corpus — a receipt is matched only against
    forms he recently opened. A posting whose application is already recorded
    stays a candidate until an inbound row exists for it: that is what heals
    the crash window between recording and logging, and what catches a
    receipt arriving after he pressed „Abgeschickt" himself. `unbekannt`
    rows (pre-v10 zombies) carry no recency and are excluded."""
    return con.execute(
        "SELECT * FROM jobs "
        " WHERE form_opened_at <> '' AND form_opened_at <> ? "
        "   AND form_opened_at >= ? "
        "   AND (bewerbung_id IS NULL "
        "        OR NOT EXISTS (SELECT 1 FROM email_log e "
        "                        WHERE e.bewerbung_id = jobs.bewerbung_id "
        "                          AND e.direction = ?))",
        (FORM_OPENED_UNKNOWN, cutoff, EMAIL_INBOUND),
    ).fetchall()


def pending_review_replies(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Inbound mail waiting for his verdict, newest first, with names."""
    return con.execute(
        "SELECT e.*, b.firma AS bewerbung_firma, b.status AS bewerbung_status, "
        "       j.company AS job_company, j.title AS job_title "
        "  FROM email_log e "
        "  LEFT JOIN bewerbungen b ON b.id = e.bewerbung_id "
        "  LEFT JOIN jobs j ON j.id = e.job_id "
        " WHERE e.direction=? AND e.needs_review=1 "
        " ORDER BY e.id DESC",
        (EMAIL_INBOUND,),
    ).fetchall()


def list_inbound_replies(con: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """The settled ledger: inbound mail already classified or filed."""
    return con.execute(
        "SELECT e.*, b.firma AS bewerbung_firma, b.status AS bewerbung_status, "
        "       j.company AS job_company, j.title AS job_title "
        "  FROM email_log e "
        "  LEFT JOIN bewerbungen b ON b.id = e.bewerbung_id "
        "  LEFT JOIN jobs j ON j.id = e.job_id "
        " WHERE e.direction=? AND e.needs_review=0 "
        " ORDER BY e.id DESC LIMIT ?",
        (EMAIL_INBOUND, limit),
    ).fetchall()


def get_email_log(con: sqlite3.Connection, email_log_id: int) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM email_log WHERE id=?", (email_log_id,)
    ).fetchone()


def classify_reply_row(
    con: sqlite3.Connection,
    email_log_id: int,
    classification: str,
    classified_by: str,
    needs_review: int,
) -> None:
    con.execute(
        "UPDATE email_log SET classification=?, classified_by=?, needs_review=? "
        " WHERE id=?",
        (classification, classified_by, int(needs_review), email_log_id),
    )


def link_reply_bewerbung(
    con: sqlite3.Connection, email_log_id: int, bewerbung_id: int | None
) -> None:
    """Point an inbound row at an application — or at none (his 'this mail
    does not belong to that application' verdict)."""
    con.execute(
        "UPDATE email_log SET bewerbung_id=? WHERE id=?",
        (bewerbung_id, email_log_id),
    )


def reopen_reply_review(con: sqlite3.Connection, email_log_id: int) -> None:
    """Put a row back on the review pile — the receipt undo path."""
    con.execute(
        "UPDATE email_log SET needs_review=1 WHERE id=?", (email_log_id,)
    )


def count_pending_replies(con: sqlite3.Connection) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM email_log WHERE direction=? AND needs_review=1",
        (EMAIL_INBOUND,),
    ).fetchone()[0]


def count_inbound_replies(con: sqlite3.Connection) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM email_log WHERE direction=?", (EMAIL_INBOUND,)
    ).fetchone()[0]


def count_recent_invitations(con: sqlite3.Connection, days: int = 7) -> int:
    """Invitations that arrived in the last week — the one inbound event the
    rail taps him on the shoulder for."""
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(days=days)).isoformat(timespec="seconds")
    return con.execute(
        "SELECT COUNT(*) FROM email_log "
        " WHERE direction=? AND classification='einladung' AND created_at>=?",
        (EMAIL_INBOUND, cutoff),
    ).fetchone()[0]


def first_answer_dates(con: sqlite3.Connection) -> dict[int, str]:
    """When each application FIRST entered an answered status, whoever wrote
    it. Uniform across hand-recorded history and ingested replies — for the
    imported rows the recording moment is the only date there is, and mixing
    'when the mail arrived' with 'when he typed it' under one column head
    would make the column mean two things."""
    placeholders = ",".join("?" * len(BEANTWORTET_STATUS))
    rows = con.execute(
        f"SELECT bewerbung_id, MIN(created_at) AS first_at FROM status_history "
        f" WHERE new_status IN ({placeholders}) GROUP BY bewerbung_id",
        tuple(sorted(BEANTWORTET_STATUS)),
    ).fetchall()
    return {int(row["bewerbung_id"]): str(row["first_at"] or "") for row in rows}


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
    # WHEN, not just how much. The rail says whether the machine is working
    # right now, and a backlog is not evidence of a worker — with AI spend
    # switched off the queue never moves and nothing else records the
    # difference between "scoring is running" and "scoring will never run".
    set_setting(con, "llm_last_call_at", _now())
