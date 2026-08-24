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

from jobdeck import apply_channel, backup, config, dates, freshness, identity, migrations
from jobdeck import settings as app_settings
from jobdeck.constants import (
    BEANTWORTET_STATUS,
    DEFAULT_DAILY_CAP,
    DEFAULT_DAILY_DRAFT_CAP,
    DRAFT_STATUS,
    EMAIL_INBOUND,
    EMAIL_INBOUND_IGNORED,
    EMAIL_OUTBOUND,
    EMAIL_OUTBOUND_TEST,
    FORM_OPENED_UNKNOWN,
    LIVENESS_GONE,
    OFFENE_STATUS,
    STATUS_RANK,
)
from jobdeck.dedupe import norm

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
    # The current duplicate gate compares companies in Python with
    # str.casefold(), because SQLite's own lower() folds ASCII only (see
    # dedupe.py). SQL grouping must use the same function to agree with that
    # legacy gate and to match "MÜLLER" with "Müller".
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


def bootstrap() -> backup.BackupResult | None:
    """Prepare storage and migrate only behind a verified recovery point.

    A new database has no prior state to preserve. Every existing database,
    including a freshly imported legacy database, must have a validated
    snapshot before migration or startup stops without changing its schema.
    """
    config.ensure_data_dirs()
    if not config.DB_PATH.exists():
        legacy = _find_legacy_db()
        if legacy is not None:
            # Consistent snapshot via the sqlite backup API — never a raw
            # file copy, the legacy DB may be open elsewhere.
            src = sqlite3.connect(
                f"{legacy.resolve().as_uri()}?mode=ro", uri=True
            )
            try:
                dst = sqlite3.connect(config.DB_PATH)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
    recovery = None
    if config.DB_PATH.exists():
        recovery = backup.run_startup_backup()
        if not recovery.ok:
            raise RuntimeError(
                recovery.error or "A verified pre-migration backup could not be created."
            )
    with db() as con:
        migrations.migrate(con)
        # Reconcile the filesystem half of an application undo interrupted by
        # process death. This runs before background workers or UI actions.
        from jobdeck.services import upload

        recovered = upload.recover_interrupted_undos(con)
        if recovered:
            log.info("upload: removed %s interrupted undo artifacts", recovered)
        # Beside it, and for the same reason: a reservation whose process died
        # would hide an employer for good. Evidence-driven — a draft still in
        # `sending` IS a live claim and keeps its company held for him to
        # resolve, which is the existing rule for a stuck send, not a new one.
        from jobdeck import attempts

        freed = attempts.reconcile_interrupted(con, _now())
        if freed:
            log.info("identity: released %s interrupted reservations", freed)
        # Self-healing, like the published_on backfill beside it: derived from
        # data the row already holds, so it is idempotent and a posting whose
        # e-mail was harvested before this rule existed stops looking like a
        # form job on the next start.
        converted = resolve_email_channels(con)
        if converted:
            log.info("apply channel: %s postings apply by e-mail", converted)
    return recovery


# --------------------------------------------------------------------------
# Applications (legacy `bewerbungen` table)
# --------------------------------------------------------------------------
def list_bewerbungen(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every application, carrying the same `last_contact` the closing rule
    uses — so the screen cannot report one silence and the rule act on another.
    """
    return con.execute(
        f"SELECT b.*, {LAST_CONTACT_SQL} AS last_contact FROM bewerbungen b"
        " ORDER BY b.gesendet_am DESC, b.id DESC"
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
    # `application_attempts.bewerbung_id` is another foreign key into the row
    # about to go, and the attempt behind a deleted application was never one:
    # the company is free again.
    con.execute(
        "UPDATE application_attempts SET state='released', bewerbung_id=NULL,"
        " updated_at=? WHERE bewerbung_id=?",
        (_now(), row_id),
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
    # `<=`, not `<`: Einladung and Absage share rank 4, so a strict
    # comparison let an automatic writer swap one settled verdict for the
    # other — and the reply reader drains a backlog, so an OLDER invitation
    # read after a newer rejection would silently reopen a closed
    # application. An automatic source may raise a status, never move it
    # sideways; a lateral correction is his to make.
    if automatic and STATUS_RANK.get(new_status, 0) <= STATUS_RANK.get(old, 0):
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


# When an employer was last in touch: a receipt they sent, or the day the
# application went out when there was none. One definition, because the
# silence rule counts from it too and two readings of "last contact" is how a
# number on a screen and the rule beneath it drift apart.
LAST_CONTACT_SQL = """COALESCE(
    (SELECT MAX(e.internal_date) FROM email_log e
      WHERE e.bewerbung_id = b.id AND e.direction = 'inbound'
        AND e.classification = 'eingang'),
    b.gesendet_am)"""

# A posting at a company that is still inside its cooling-off window. Such a
# posting cannot become an application yet, so it leaves the working list on
# the same terms as a score-0 mismatch or an offline ad — counted beneath the
# list, one click away, never deleted, and back on its own once the window
# passes. Policy: `docs/adr/0010-company-cooling-off-window.md`.
#
# The SQL mirror of `identity.decide`'s cooling-off arm, because the filter has
# to run where the paging and the counts do. A differential test pins the two
# equal over a generated corpus — two hand-written copies of one rule drift,
# and this one decides whether a posting is seen at all.
#
# The contact-address arm this filter used to carry is gone. ADR 0002 keeps a
# shared address as evidence and never as an identity, and a mailbox serving
# two employers would otherwise hide the second one's postings behind the
# first one's application.
#
# One bound parameter: the day on or before which an application stops holding
# its company. `applied_firm_params` derives it, and returns a date beyond any
# real one when the window is switched off, which makes the whole arm false
# rather than needing a second shape of the query.
#
# The date compared is the LAST CONTACT, not the day the application was
# sent. A ledger row can be months old while the conversation is days old —
# one real row read as sent in June had a receipt from August — and counting
# from the send date then offers a company that answered last week.
#
# An application whose date is missing or unreadable sorts to that same far
# future and therefore keeps holding: it cannot prove its window has passed,
# and assuming it has would offer a company that may have been written to
# yesterday. The Python rule reads it the same way.
#
# Written as an UNCORRELATED IN-subquery on purpose. The obvious
# `EXISTS (... WHERE jd_norm(b.firma) = jd_norm(jobs.company))` makes SQLite
# call jd_norm — a Python callback — once per (posting, application) pair:
# measured at 330 ms over a real corpus, and the inbox pays the filter three
# times per page load. Uncorrelated, each side is folded once and matched
# through an ephemeral index.
APPLIED_FIRM_SQL = f"""(
    jd_norm(jobs.company) <> '' AND jd_norm(jobs.company) IN (
        SELECT jd_norm(b.firma) FROM bewerbungen b
         WHERE jd_norm(b.firma) <> ''
           AND COALESCE(NULLIF(SUBSTR({LAST_CONTACT_SQL}, 1, 10), ''),
                        '9999-12-31') > ?))"""

# Beyond any date a posting or an application can carry, so "unknown" and
# "the window is off" are both expressed as a comparison instead of as a
# second query shape.
_FAR_FUTURE = "9999-12-31"


def applied_firm_params(con: sqlite3.Connection) -> tuple[str]:
    """The bindings `APPLIED_FIRM_SQL` needs, derived from the setting.

    With the window switched off every application is compared against the far
    future, so none of them holds and the filter hides nothing — which is what
    switching the rule off has to mean.
    """
    days = app_settings.integer(
        con, identity.COOLDOWN_SETTING, identity.DEFAULT_COOLDOWN_DAYS,
        minimum=0,
    )
    if days <= identity.WINDOW_OFF:
        held_since = _FAR_FUTURE
    else:
        held_since = (
            datetime.date.today() - datetime.timedelta(days=days)
        ).isoformat()
    return (held_since,)


# The same position at the same company as an application already in the
# ledger — the employer's repost of the very advert that produced it, or an
# identical opening. It can never become a second application, so it leaves the
# working list on the same terms as a score-0 mismatch: counted beneath the
# list, one click away, never deleted. Unlike the cooling-off pile it never
# comes back, because waiting changes nothing about it.
#
# The SQL mirror of `identity.republication_of`, pinned equal to it by a
# differential test. Both sides require a non-empty company AND a non-empty
# position: two rows that failed to store a title are not the same role.
#
# Those two emptiness checks are REDUNDANT with each other, deliberately, and
# a mutation of either one alone survives the suite: an empty title cannot
# equal a non-empty position, and vice versa. Removing BOTH does change the
# answer, and the differential test catches that. Defence in depth, not a hole
# in the tests — the same shape as the two guards in `resolve_email`.
#
# One composite key rather than a correlated EXISTS, for the reason the filter
# above gives — a correlated form calls the `jd_norm` Python callback once per
# (posting, application) pair. `char(31)` is the unit separator, which `norm`
# cannot leave in a value: it collapses every whitespace run to a space and
# drops the invisible categories, so no company name and no title can carry
# one and fake a match across the join.
REPUBLICATION_SQL = """(
    jd_norm(jobs.company) <> '' AND jd_norm(jobs.title) <> ''
    AND jd_norm(jobs.company) || char(31) || jd_norm(jobs.title) IN (
        SELECT jd_norm(b.firma) || char(31) || jd_norm(a.position)
          FROM application_attempts a
          JOIN bewerbungen b ON b.id = a.bewerbung_id
         WHERE a.position <> '' AND jd_norm(b.firma) <> ''))"""

# A posting at a company he has hidden. Written as an UNCORRELATED IN, like
# the gate above and for the same measured reason: the natural EXISTS form
# calls the jd_norm Python callback once per (posting, company) pair, which
# cost 330 ms against 6 ms and is paid on every page load.
#
# The empty-company arm is not defensive tidiness. `_COMPANY_KEY_SQL` gives a
# posting with no company its OWN key ('job:<id>'), because a blank field is
# missing information rather than an employer — so it can never be part of a
# company, and `jd_norm('') IN (…)` must not match an accidentally empty row
# in the table either.
HIDDEN_FIRM_SQL = """(
    jd_norm(jobs.company) <> '' AND jd_norm(jobs.company) IN (
        SELECT company_key FROM hidden_companies WHERE company_key <> ''))"""

# The floor he sets under the list. Three things it must get right, and each of
# them was a way to lie to him:
#
# * It compares the AGED score — the very number the row prints and the list
#   sorts on. Filtering the raw `match_score` instead would put a row reading
#   "58" above a floor of 60, and the two figures on one screen would disagree.
# * `match_score IS NULL` passes. `NULL >= 40` is NULL, so without this arm a
#   posting that arrived between a search and the scoring pass would vanish
#   silently — which is exactly what a screen must never do to a row nobody has
#   judged yet.
# * A score of 0 is NOT a low score. It means the posting violates a hard
#   requirement, and it has its own named pile; letting it through a floor of
#   "ab 0" would tip 564 knock-outs into the working list.
SCORE_FLOOR_SQL = (
    f"({freshness.effective_score_sql()} >= ? OR match_score IS NULL)"
)


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
    con: sqlite3.Connection,
    status: str | None, mismatches: str, gone: str, applied: str = "include",
    old: str = "include", stale_age_days: int = freshness.DEFAULT_STALE_AGE_DAYS,
    bookmarked: str = "include", opened: str = "include",
    in_progress: str = "include", search: str = "",
    keep_ids: tuple[int, ...] = (), hidden: str = "include",
    min_score: int = 0, republication: str = "include",
) -> tuple[list[str], list]:
    """WHERE fragments + bound values shared by the list and the count, so a
    page can never be filtered differently from the total printed beside it.

    Takes the connection only to derive the cooling-off cutoff once, here,
    rather than in each of the five callers — five derivations of one date is
    five chances for the list and the count beside it to disagree.

    An unrecognised filter value raises rather than being ignored: silently
    falling through would SHOW a pile the caller asked to hide, and a hidden
    pile exists precisely because its rows should not be acted on."""
    for name, value in (("mismatches", mismatches), ("gone", gone),
                        ("applied", applied), ("old", old),
                        ("bookmarked", bookmarked), ("opened", opened),
                        ("in_progress", in_progress), ("hidden", hidden),
                        ("republication", republication)):
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
        params.extend(applied_firm_params(con))
    elif applied == "only":
        where.append(APPLIED_FIRM_SQL)
        params.extend(applied_firm_params(con))
    # Its own pile, not folded into the one above: that one is a company held
    # back until a day it names, this one is a position that is finished. A
    # single count covering both would print "zurückgestellt" over rows that
    # are never coming back.
    if republication == "exclude":
        where.append(f"NOT ({REPUBLICATION_SQL})")
    elif republication == "only":
        where.append(REPUBLICATION_SQL)
    # His decision, not a fact about the posting — but unlike "kein Interesse"
    # it is about the COMPANY, so it keeps reaching postings that did not exist
    # when he pressed. That is the whole point: he pressed three times on one
    # staffing agency because each press only reached one advert.
    if hidden == "exclude":
        where.append(f"NOT {HIDDEN_FIRM_SQL}")
    elif hidden == "only":
        where.append(HIDDEN_FIRM_SQL)
    # Bound, and appended together with its value — the params list is
    # positional, so a clause added without its binding shifts every later one.
    if min_score > 0:
        where.append(SCORE_FLOOR_SQL)
        params.append(min_score)
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


# The current list groups postings by company to match the legacy duplicate
# gate. The accepted product policy distinguishes posting identity from
# company-and-position identity; see
# `docs/adr/0002-application-identity-and-duplicate-policy.md`. An empty company
# name groups with nothing (its own id is the key): a blank field is missing
# data, not a shared company. `jd_norm` is dedupe.norm itself (registered in
# connect()), so list grouping and the current gate use the same normalization.
_COMPANY_KEY_SQL = (
    "CASE WHEN jd_norm(company)='' THEN 'job:'||id "
    "ELSE 'firma:'||jd_norm(company) END"
)
# Which posting REPRESENTS a company inside the grouped list. FIXED, and
# deliberately not his to choose: the row stands for the whole company, so it
# has to be the best one it holds. Letting the sort control reach in here would
# mean that switching to "newest first" quietly re-elected the newest advert of
# a nineteen-advert staffing agency to speak for it — a ranking regression
# invisible from the screen, because the row still looks like a company.
_RANK_ORDER_SQL = "effective_score DESC NULLS LAST, published_on DESC, id DESC"

# The order the LIST is drawn in — his choice, and only his choice. Both orders
# keep the other key as their tie-break, so neither is a pure one-dimensional
# sort: with 222 of 300 rows under 40 points, "newest first" alone would put
# fresh noise at the top, which is the complaint it is meant to answer.
#
# An unknown date is stored as the EMPTY STRING, not as NULL, and it still
# sorts last — checked rather than assumed, and written down here because the
# obvious "fix" is a NULLIF that changes nothing: '' collates below any date,
# so DESC already puts it at the bottom, exactly where "we do not know" belongs.
# `NULLS LAST` covers the NULL a hand-edited row could carry.
_DATE_ORDER_SQL = ("published_on DESC NULLS LAST, "
                   "effective_score DESC NULLS LAST, id DESC")
# The same order for a list of COMPANIES. A row stands for a company's BEST
# advert, so ordering by that advert's date puts a company whose newest advert
# went up today at thirty days old because its best one is. "Neueste zuerst"
# has to mean the company's newest advert, whichever of its adverts represents
# it — which is what the window over the whole partition answers.
_GROUP_DATE_ORDER_SQL = ("company_published_on DESC NULLS LAST, "
                         "effective_score DESC NULLS LAST, id DESC")
LIST_ORDERS = {"score": _RANK_ORDER_SQL, "date": _DATE_ORDER_SQL}
_GROUP_ORDERS = {"score": _RANK_ORDER_SQL, "date": _GROUP_DATE_ORDER_SQL}
DEFAULT_LIST_ORDER = "date"


def list_order_sql(sort: str, grouped: bool = False) -> str:
    """The ORDER BY for a named sort, falling back rather than raising.

    The name reaches here from a stored setting he can edit; an unknown one is
    a screen that will not open, and the default is always a safe answer."""
    orders = _GROUP_ORDERS if grouped else LIST_ORDERS
    return orders.get(sort or "", orders[DEFAULT_LIST_ORDER])


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
        # The company's newest advert, so "Neueste zuerst" can mean the
        # company rather than whichever advert represents it.
        " MAX(published_on) OVER company AS company_published_on,"
        # COUNT over the RANKING window would be a running total: a window with
        # an ORDER BY frames rows up to the current one, so the best-ranked row
        # of every company would report a count of 1. The count needs a window
        # with no ordering, which frames the whole partition.
        " COUNT(*) OVER company AS company_count"
        " FROM filtered"
        f" WINDOW ranking AS (PARTITION BY company_key ORDER BY {_RANK_ORDER_SQL}),"
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
    hidden: str = "include",
    republication: str = "include",
    min_score: int = 0,
    sort: str = DEFAULT_LIST_ORDER,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """One row per company: its best-ranked posting, plus `company_count`.

    Two orders, and they answer different questions. WITHIN a company the
    ranking is always by score, because something has to choose which posting
    represents it and a row that stands for a whole company should be its best.
    BETWEEN companies, "newest first" means the company's newest advert — not
    the date of whichever advert represents it, or a company that posted today
    would sort at thirty days because its strongest advert is that old."""
    where, params = _job_filters(con, status, mismatches, gone, applied, old,
                                 stale_age_days, bookmarked, opened,
                                 in_progress, search, keep_ids, hidden,
                                 min_score, republication)
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    order = list_order_sql(sort, grouped=True)
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
    hidden: str = "include",
    republication: str = "include",
    min_score: int = 0,
    sort: str = DEFAULT_LIST_ORDER,
) -> int:
    """How many companies the grouped view holds."""
    where, params = _job_filters(con, status, mismatches, gone, applied, old,
                                 stale_age_days, bookmarked, opened,
                                 in_progress, search, keep_ids, hidden,
                                 min_score, republication)
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
    hidden: str = "include",
    republication: str = "include",
    min_score: int = 0,
    sort: str = DEFAULT_LIST_ORDER,
    per_company: int = SIBLINGS_PER_COMPANY,
) -> list[sqlite3.Row]:
    """The postings a grouped row stands in front of, best-ranked first.

    Asked only for the companies on the current page, and capped per company:
    one employer posting fifty near-identical roles must not decide how much a
    page renders. The caller has `company_count` to say how many there really
    are."""
    if not company_keys:
        return []
    where, params = _job_filters(con, status, mismatches, gone, applied, old,
                                 stale_age_days, bookmarked, opened,
                                 in_progress, search, keep_ids, hidden,
                                 min_score, republication)
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
    hidden: str = "include",
    republication: str = "include",
    min_score: int = 0,
    sort: str = DEFAULT_LIST_ORDER,
) -> int:
    """How many postings a `list_jobs` call with the same filters would have,
    ignoring its page limit — the total a paged view has to print."""
    where, params = _job_filters(con, status, mismatches, gone, applied, old,
                                 stale_age_days, bookmarked, opened,
                                 in_progress, search, keep_ids, hidden,
                                 min_score, republication)
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
    hidden: str = "include",
    republication: str = "include",
    min_score: int = 0,
    sort: str = DEFAULT_LIST_ORDER,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """List postings. mismatches: 'include' (default), 'exclude' (hide the
    score-0 rows, NULL-safe so unscored postings stay visible) or 'only'
    (just the hidden pile — keeps mismatches reachable regardless of how
    many better-scored rows fill the page limit). `gone` takes the same three
    values over postings whose ad the source says is no longer there."""
    where, params = _job_filters(con, status, mismatches, gone, applied, old,
                                 stale_age_days, bookmarked, opened,
                                 in_progress, search, keep_ids, hidden,
                                 min_score, republication)
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    # The age-adjusted score is SELECTED as well as ordered on, so the number
    # the UI prints is the very number that decided the row's position — two
    # copies of that rule would drift (see freshness.py).
    derived = (f"{freshness.AGE_SQL} AS age_days, "
               f"{freshness.effective_score_sql()} AS effective_score, "
               f"{_DRAFT_STATUS_SQL} AS draft_status, "
               f"{_DRAFT_UPDATED_SQL} AS draft_updated_at, "
               f"{_DRAFT_PDF_SQL} AS pdf_path")
    order = list_order_sql(sort)
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

    `kind` is written by the legacy build and never inferred from the file
    system. Empty means no complete legacy package is staged. Versioned,
    job-specific document selection is the accepted target described in
    `docs/adr/0005-job-specific-application-documents.md`."""
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
        # A company he has hidden is one he will not apply to, and scoring is
        # a paid call: the batch would go on spending haiku on every advert a
        # nineteen-branch staffing agency posts, for ever.
        f" AND NOT {HIDDEN_FIRM_SQL}"
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
           AND NOT ({HIDDEN_FIRM_SQL.replace("jobs.", "j.")})
         ORDER BY effective_score DESC, j.published_on DESC, j.id DESC
         LIMIT ?
        """,
        (min_score, max_age_days, 1 if include_forms else 0,
         *PREPARED_DRAFT_STATUS, *applied_firm_params(con), limit),
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


def count_republication_jobs(
    con: sqlite3.Connection, status: str | None = None
) -> int:
    """How many postings repeat a position that already has an application."""
    sql = f"SELECT COUNT(*) FROM jobs WHERE {REPUBLICATION_SQL}"
    params: tuple = ()
    if status:
        sql += " AND status=?"
        params = (status,)
    return con.execute(sql, params).fetchone()[0]


def count_applied_firm_jobs(con: sqlite3.Connection, status: str | None = None) -> int:
    """How many postings the already-applied filter would hide for this view."""
    sql = f"SELECT COUNT(*) FROM jobs WHERE {APPLIED_FIRM_SQL}"
    params: tuple = applied_firm_params(con)
    if status:
        sql += " AND status=?"
        params = (*params, status)
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


# What a person can type in from a posting they are looking at. `firma` is
# deliberately absent: the company name is the dedupe key the send gate reads,
# and letting it be edited here would let one posting quietly become a
# different company's.
CONTACT_FIELDS = ("contact_email", "ansprechpartner", "contact_strasse",
                  "contact_plz_ort", "contact_phone")

CONTACT_SOURCE_USER = "user"


def set_contact_details(con: sqlite3.Connection, job_id: int,
                        values: dict, source: str = CONTACT_SOURCE_USER) -> None:
    """Record contact details a human read off the posting itself.

    The Arbeitsagentur puts an employer's address behind a CAPTCHA, so for a
    large part of the corpus the only reader who can ever see it is him —
    on a real corpus this is a large minority of the open BA postings, many
    of them well scored. Without this the letter greets
    "Sehr geehrte Damen und Herren", the recipient block stays blank and the
    send path has nobody to send to, on postings that are e-mail applications
    in every respect except that the app could not be told so.

    Only the keys present in `values` are written, so correcting one field
    never blanks the others. Writing an address also settles the channel (see
    `set_contact_email`): a posting stops being a form job the moment there is
    somewhere to write to.
    """
    updates = {k: str(values[k]).strip() for k in CONTACT_FIELDS if k in values}
    if not updates:
        return
    assignments = ", ".join(f"{column}=?" for column in updates)
    con.execute(
        f"UPDATE jobs SET {assignments}, contact_source=? WHERE id=?",
        (*updates.values(), source, job_id),
    )
    if "contact_email" in updates:
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
    """New postings still waiting for a match score — the scoring backlog.

    Counted the way the batch SELECTS them, or the Puls would pulse for ever
    over adverts nothing is going to score."""
    return con.execute(
        "SELECT COUNT(*) FROM jobs WHERE status='new' AND match_score IS NULL"
        f" AND NOT {HIDDEN_FIRM_SQL}"
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


# Hiding a company removes rows from every list on the pipeline pages, and it
# is not a write to `jobs` — so without this term the list he just pruned would
# keep showing the company until something unrelated happened to change.
#
# MAX(id), not MAX(hidden_at) and not MAX(rowid). The timestamp has one-second
# resolution, so hiding a company and taking it back inside the same second
# would compare EQUAL to never having touched it. A bare rowid is no better: a
# rowid table without AUTOINCREMENT assigns max(rowid)+1 over the rows PRESENT,
# so releasing the NEWEST hidden company and hiding another lands on the same
# number — and that pair is exactly what the undo bar produces.
_HIDDEN_SIGNATURE_SQL = (
    "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM hidden_companies"
)


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
        *con.execute(_HIDDEN_SIGNATURE_SQL).fetchone(),
    )


def hide_company(con: sqlite3.Connection, company: str,
                 source: str = "user") -> str:
    """Never show this employer again, and answer with the key it was filed
    under. A company already hidden is left as it was rather than re-stamped:
    pressing twice is not a second decision.

    Returns '' for a posting with no company — that is missing information, not
    an employer, and hiding "everything with a blank name" would take out rows
    that have nothing to do with each other.
    """
    key = norm(company or "")
    if not key:
        return ""
    con.execute(
        "INSERT OR IGNORE INTO hidden_companies (company_key, company, "
        "hidden_at, source) VALUES (?, ?, ?, ?)",
        (key, (company or "").strip(), _now(), source))
    con.commit()
    return key


def unhide_company(con: sqlite3.Connection, company_key: str) -> None:
    """Take a company back. The postings were never touched, so they simply
    reappear — including the ones discovered while it was hidden."""
    con.execute("DELETE FROM hidden_companies WHERE company_key=?",
                (company_key,))
    con.commit()


def list_hidden_companies(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """The hidden companies, newest first, each with how many postings of its
    own are currently out of sight — the figure that makes taking one back a
    decision rather than a guess."""
    return con.execute(
        "SELECT h.*, ("
        "  SELECT COUNT(*) FROM jobs j"
        "  WHERE jd_norm(j.company)=h.company_key"
        ") AS hidden_jobs "
        "FROM hidden_companies h ORDER BY h.id DESC").fetchall()


def count_hidden_companies(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) FROM hidden_companies").fetchone()[0]


def is_company_hidden(con: sqlite3.Connection, company: str) -> bool:
    key = norm(company or "")
    if not key:
        return False
    return con.execute(
        "SELECT 1 FROM hidden_companies WHERE company_key=?",
        (key,)).fetchone() is not None


def company_cost(con: sqlite3.Connection, company: str) -> dict:
    """How many open adverts this employer holds, and the best score among
    them — what the undo bar has to NAME so hiding is a decision.

    One aggregate over the table. It used to page 1000 rows out of `list_jobs`
    and filter them in Python, which on his corpus is a CAP (1080 open
    postings) applied to a list ordered by DATE — so the "best" it reported
    was the best of whatever the cap happened to keep.
    """
    key = norm(company or "")
    if not key:
        return {"jobs": 0, "best": 0}
    row = con.execute(
        f"SELECT COUNT(*), COALESCE(MAX({freshness.effective_score_sql()}), 0)"
        " FROM jobs WHERE status='new' AND jd_norm(company)=?",
        (key,)).fetchone()
    return {"jobs": row[0], "best": row[1]}


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


# A receipt or an out-of-office answer leaves an application exactly as
# unanswered as it was — nobody has decided anything — but it does prove
# someone is there, so it restarts the clock. Any OTHER inbound verdict means
# a human engaged, and such a row must never be closed as unanswered.
#
# The empty verdict is deliberately NOT here. An inbound mail the classifier
# could not place means "an employer wrote and we do not know what they said",
# which is the opposite of knowing nobody answered — so it blocks the close
# and waits for him. Measured when this was written: 8 such mails exist, 7 of
# them linked, all on applications already closed, so the guard changes
# nothing today and is purely prospective.
SILENT_CLASSIFICATIONS = ("eingang", "auto")

# What may restart the silence clock: a receipt, and nothing else.
#
# A receipt acknowledges HIS application — somebody's system has it, so the
# waiting genuinely starts again from there. An out-of-office does not: it
# says the reader is away, not that the application was seen, and he asked for
# a rule about not having been answered.
#
# Keeping 'auto' out of here also closes a hole the review panel confirmed:
# `replies.is_auto_submitted` files ANY message carrying List-Unsubscribe or
# Precedence: bulk under 'auto', so an employer newsletter matched to an
# application by name or domain would have reset the clock every month and the
# row would never have closed — the exact row the rule exists for. The headers
# that would tell a newsletter from an out-of-office are not stored, so the
# distinction cannot be made after ingestion; not restarting on either is the
# reading that needs no header.
CLOCK_RESTARTING_CLASSIFICATIONS = ("eingang",)

# Since when an application has been silent — the ONE definition of it.
#
# Stated once because the rule and the three screens that report it must not
# drift: they already had, and on the real register thirteen of fifty-seven
# open rows printed a number the rule did not use — one of them 69 days beside
# a threshold of 60, and still open, with nothing on the page explaining why.
_SILENT_APPLICATIONS_SQL = """
SELECT b.id, b.firma, b.status, b.gesendet_am, b.kanal,
       {last_contact} AS last_contact
  FROM bewerbungen b
 WHERE b.status IN ({open_status})
   AND b.gesendet_am IS NOT NULL AND b.gesendet_am <> ''
   AND NOT EXISTS (
        SELECT 1 FROM email_log e
         WHERE e.bewerbung_id = b.id AND e.direction = 'inbound'
           AND e.classification NOT IN ({silent})
       )
   AND julianday(?) - julianday({last_contact}) >= ?
 ORDER BY last_contact ASC
"""


def silent_applications(con: sqlite3.Connection, days: int) -> list[sqlite3.Row]:
    """Open applications nobody has really answered for at least `days`.

    Every channel counts: an application sent through a form has no address
    to be answered at, which makes its silence more final rather than less.
    An application that drew any real verdict is excluded outright — closing
    one of those as unanswered would contradict what the employer said.
    """
    open_status = ",".join("?" * len(OFFENE_STATUS))
    silent = ",".join("?" * len(SILENT_CLASSIFICATIONS))
    sql = _SILENT_APPLICATIONS_SQL.format(
        open_status=open_status, silent=silent, last_contact=LAST_CONTACT_SQL)
    return con.execute(
        sql,
        (*sorted(OFFENE_STATUS), *SILENT_CLASSIFICATIONS,
         _now(), int(days)),
    ).fetchall()


def send_mode(con: sqlite3.Connection) -> dict:
    """Whether a send would be real, and what today's budget has left.

    Shared because two screens now stand in front of the same send: the review
    queue's banner and the pre-send confirmation wherever it is opened from.
    """
    return {
        "real": app_settings.boolean(con, "real_send_enabled", False),
        "test_recipient": get_setting(con, "test_recipient", "").strip(),
        "cap": app_settings.integer(
            con, "daily_send_cap", int(DEFAULT_DAILY_CAP), minimum=0
        ),
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
             body_text, job_id, matched_note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            values.get("matched_note", ""),
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
    return app_settings.integer(con, _DRAFT_COUNT_KEY, 0, minimum=0)


def daily_draft_cap(con: sqlite3.Connection) -> int:
    """How many letters may be written in a day. His decision, 2026-08-15: a
    HARD cap, raised deliberately in Einstellungen rather than by a one-press
    override, because an override always one press away is not a limit."""
    return app_settings.integer(
        con,
        "daily_draft_cap",
        int(DEFAULT_DAILY_DRAFT_CAP),
        minimum=0,
        allow_decimal=True,
    )


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
    """Write the application for a posting. Returns the bewerbung id, or None
    if the posting is gone.

    A writer, not a gate. Whether an application MAY be made is decided by
    `identity` and claimed by `attempts`, inside the caller's transaction and
    before any of this runs — see
    `docs/adr/0010-company-cooling-off-window.md`. The rule used to live here,
    which meant the SQL layer owned a product decision and a temporary hold
    was written as the permanent status `duplicate`.
    """
    job = get_job(con, job_id)
    if job is None:
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


def mark_duplicate_of(
    con: sqlite3.Connection, job_id: int, bewerbung_id: int
) -> None:
    """File a posting as the duplicate of an application that already exists.

    Only for a PERMANENT refusal. A cooling-off window is temporary and must
    leave the posting `new`, or waiting it out would never bring it back — the
    read-time filter hides it meanwhile.
    """
    set_job_status(con, job_id, "duplicate")
    con.execute("UPDATE jobs SET duplicate_of=? WHERE id=?", (bewerbung_id, job_id))


def unrecord_application(
    con: sqlite3.Connection,
    job_id: int,
    bewerbung_id: int,
    previous_status: str,
) -> None:
    """Undo the database writes made by ``apply_job``.

    The caller owns the transaction so filesystem staging and the upload
    pointer can be coordinated with these writes. No commit occurs here.

    Four writes, because `apply_job` made four: `add_bewerbung` writes a
    `status_history` row of its own, and `set_job_status` sets both the status
    and the link. `previous_status` is what the posting was BEFORE — restoring
    a hardcoded 'new' would quietly revive a posting he had skipped.
    """
    # The posting first: `jobs.bewerbung_id` is a foreign key into the row
    # about to be deleted, so deleting it while the link stands raises.
    changed = con.execute(
        "UPDATE jobs SET status=?, bewerbung_id=NULL "
        "WHERE id=? AND bewerbung_id=?",
        (previous_status, job_id, bewerbung_id),
    )
    if changed.rowcount != 1:
        raise RuntimeError("the application is no longer linked to this posting")
    # `jobs.duplicate_of` is a SECOND foreign key into the row being deleted,
    # and it is written by the very gate this application armed. Refused
    # postings are not duplicates of an application that no longer exists.
    con.execute(
        "UPDATE jobs SET status='new', duplicate_of=NULL "
        " WHERE duplicate_of=? AND bewerbung_id IS NULL",
        (bewerbung_id,),
    )
    # `email_log.bewerbung_id` is another foreign key into the application.
    con.execute(
        "UPDATE email_log SET bewerbung_id=NULL WHERE bewerbung_id=?",
        (bewerbung_id,),
    )
    con.execute("DELETE FROM status_history WHERE bewerbung_id=?",
                (bewerbung_id,))
    # Filing also linked the draft; remove that link before deleting the row.
    unfile_draft(con, bewerbung_id)
    con.execute("DELETE FROM bewerbungen WHERE id=?", (bewerbung_id,))


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
    # Any logged message of this thread that names an application anchors
    # it — outbound OR a receipt already read into the same thread. Only
    # outbound rows were consulted at first, which left every FORM
    # application unreachable: it sent nothing, so its thread's only anchor
    # is the inbound receipt. Half his applications go out that way, and
    # their eventual rejection was being dropped as unmatched.
    # `bewerbung_id IS NOT NULL` is what keeps rehearsal traffic out: a test
    # send never records one.
    row = con.execute(
        "SELECT bewerbung_id FROM email_log "
        " WHERE gmail_thread_id=? AND bewerbung_id IS NOT NULL "
        " ORDER BY id DESC LIMIT 1",
        (thread_id,),
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


def count_skipped_messages(con: sqlite3.Connection) -> int:
    """How many messages were read once, matched nothing, and are now closed
    to every future pass. The number the rescan control exists to answer."""
    row = con.execute(
        "SELECT COUNT(*) FROM email_log WHERE direction=?",
        (EMAIL_INBOUND_IGNORED,),
    ).fetchone()
    return int(row[0]) if row else 0


def forget_ignored_messages(con: sqlite3.Connection) -> int:
    """Drop the opaque ids of messages no application could be found for.

    Those rows exist so a bounded pass can advance past a backlog without
    reading the same message for ever — which also means a message skipped
    once is skipped for good, and every later improvement to the matching or
    the German rules reaches only mail that has not arrived yet. Forgetting
    them is what makes an improvement retroactive: the next pass sees them as
    new again.

    Only the ignored rows. A message already tied to an application keeps its
    row, so re-listing cannot file it twice."""
    cur = con.execute("DELETE FROM email_log WHERE direction=?",
                      (EMAIL_INBOUND_IGNORED,))
    return cur.rowcount


def bewerbungen_for_name_match(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every application, address or not — the company-name arm's candidates.

    Deliberately separate from bewerbungen_for_reply_match: that one excludes
    address-less rows because the arms it feeds compare addresses, and this
    one exists precisely FOR those rows. A form application at a portal has
    no address the employer will ever write from, so its company name is the
    only thing a reply can be tied to."""
    return con.execute(
        "SELECT id, firma, status FROM bewerbungen "
        " WHERE COALESCE(firma,'') <> '' ORDER BY id DESC"
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


def set_reply_matched_by(
    con: sqlite3.Connection, email_log_id: int, matched_by: str
) -> None:
    """Restate HOW an inbound row reached its application.

    Written after the fact by the one path that can change the answer:
    adopting a receipt onto an application that appeared meanwhile attaches
    rather than records, and `matched_by` is what decides whether an undo may
    later delete a ledger row."""
    con.execute(
        "UPDATE email_log SET matched_by=? WHERE id=?",
        (matched_by, email_log_id),
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
    """Compatibility reader for settings that are intentionally raw text."""
    return app_settings.text(con, key, default)


def set_setting(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def ai_enabled(con: sqlite3.Connection) -> bool:
    """Master switch for all LLM spend. Off by default — the user opts in
    from Settings; every service that calls the LLM must check this first."""
    return app_settings.boolean(con, "ai_enabled", False)


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
