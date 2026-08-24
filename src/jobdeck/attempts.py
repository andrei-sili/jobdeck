"""Persistent application attempts: the reservation every write path takes.

`identity` decides; this module is where that decision meets the database and
becomes a claim other connections can see.

The distinction is the whole point of the slice. Before it, the e-mail path
checked the gate, set the draft to `sending`, released its transaction, and
then spent seconds inside a Gmail call before writing the ledger row. During
those seconds nothing recorded the intent, so the form path asked the same
question, got the same answer, and wrote a second application to one company.
A bigger lock would not have helped: the two paths do not share a process
boundary worth locking, and the second one may run in a background thread.

A reservation is a row. Committed inside the caller's ``BEGIN IMMEDIATE``, it
is visible to every other connection the moment the transaction ends, and the
``UNIQUE`` constraint on the key — not any in-process lock — is what makes two
attempts for one posting impossible.

Every function here takes the caller's connection and never commits: the
reservation has to land in the same transaction as the state change it guards,
or the window it closes simply moves.
"""

from __future__ import annotations

import datetime
import sqlite3

from jobdeck import db, identity, settings
from jobdeck.dedupe import norm

# States a row can hold. `released` is kept rather than deleted: "he started
# here and stopped" is evidence, and a deleted row cannot explain why a key
# was once taken.
RESERVED = "reserved"
RECORDED = "recorded"
RELEASED = "released"

# Re-exported from `identity`, which owns the policy: the SQL filter in `db`
# reads the same two names, and a second default is a second rule.
COOLDOWN_SETTING = identity.COOLDOWN_SETTING
DEFAULT_COOLDOWN_DAYS = identity.DEFAULT_COOLDOWN_DAYS

# The ledger stays the source of truth for "did an application go out", and
# the attempt table only supplies the position it never stored. Read this way
# round, dropping the new table degrades the gate to today's behaviour rather
# than opening it.
# `last_contact` comes from `db.LAST_CONTACT_SQL`, the one definition the
# silence rule already counts from. Two definitions of "when was this employer
# last in touch" is how the number on a screen and the rule beneath it drift
# apart, and this project has paid for that twice.
_APPLICATIONS_SQL = """
SELECT b.id                            AS id,
       COALESCE(b.firma, '')           AS company,
       COALESCE(b.email, '')           AS email,
       COALESCE(b.gesendet_am, '')     AS sent_on,
       {last_contact}                  AS last_contact,
       COALESCE((SELECT a.position FROM application_attempts a
                  WHERE a.bewerbung_id = b.id AND a.position <> ''
                  ORDER BY a.id LIMIT 1), '') AS position
  FROM bewerbungen b
"""

_LIVE_SQL = """
SELECT idempotency_key AS key, company, channel, job_id
  FROM application_attempts
 WHERE state = 'reserved'
"""


def stamp() -> str:
    """The timestamp format the rest of the database already uses."""
    return datetime.datetime.now().isoformat(timespec="seconds")


def key_for_job(job_id: int) -> str:
    """The one key a posting's attempt is ever filed under.

    Derived from the posting rather than from the channel, because the rule it
    enforces is "one attempt per posting" — an e-mail attempt and a form
    attempt for the same posting must collide, not coexist.
    """
    return f"job:{int(job_id)}"


def cooldown_days(con: sqlite3.Connection) -> int:
    """How long a company is left alone after an application went to it."""
    return settings.integer(
        con, COOLDOWN_SETTING, DEFAULT_COOLDOWN_DAYS, minimum=0
    )


def applications(con: sqlite3.Connection) -> list[identity.Application]:
    return [
        identity.Application(
            id=row["id"], company=row["company"], email=row["email"],
            position=row["position"], sent_on=row["sent_on"],
            last_contact=str(row["last_contact"] or ""),
        )
        for row in con.execute(
            _APPLICATIONS_SQL.format(last_contact=db.LAST_CONTACT_SQL)
        )
    ]


def live_reservations(con: sqlite3.Connection) -> list[identity.Reservation]:
    """Every claim currently held, including the caller's own.

    A posting whose own attempt is in flight must report that, not report
    itself free: `decide_for_job` is what screens ask, and answering "you may
    apply" beside a message already leaving is the kind of disagreement
    between screen and gate this slice exists to end.
    """
    return [
        identity.Reservation(
            key=row["key"], company=row["company"],
            channel=row["channel"], job_id=row["job_id"],
        )
        for row in con.execute(_LIVE_SQL)
    ]


def authorizations(con: sqlite3.Connection) -> set[int]:
    """Postings the candidate has explicitly cleared to apply to anyway.

    A standing authorization rather than a live claim, so it survives the
    several presses an application takes: by e-mail the letter is written
    first and sent minutes later, and a confirmation that expired in between
    would refuse him at the last gate with no way to say yes again.

    Stored on the attempt row as a confirmation stamp while the row itself
    stays `released` — it authorizes, it does not reserve. That keeps "one
    attempt per posting" exactly as strict: the row is still claimed by
    whichever writer revives it first, inside `BEGIN IMMEDIATE`.
    """
    return {
        row["job_id"]
        for row in con.execute(
            "SELECT job_id FROM application_attempts "
            " WHERE override_confirmed_at <> '' AND job_id IS NOT NULL"
        )
    }


def _lifted(decision: identity.Decision, authorized: bool) -> identity.Decision:
    """A cooling-off hold the candidate has already answered is not a hold.

    Only that one. A republication and a live reservation stand: one is a
    mistake no confirmation makes reasonable, the other may already be
    leaving.
    """
    if authorized and decision.verdict == identity.COOLING_OFF:
        return identity.Decision(
            verdict=identity.ALLOW,
            application_id=decision.application_id,
            position=decision.position,
            sent_on=decision.sent_on,
            corroborating_email=decision.corroborating_email,
        )
    return decision


def _posting(job: sqlite3.Row | dict) -> identity.Posting:
    """A posting row as identity needs to see it.

    Column-by-column rather than by construction, because callers pass rows
    from several queries and a narrower SELECT must degrade to an empty field
    instead of raising out of a gate.
    """
    keys = job.keys() if isinstance(job, sqlite3.Row) else job

    def field(name: str) -> str:
        return str((job[name] if name in keys else "") or "")

    job_id = job["id"] if "id" in keys else None
    return identity.Posting(
        company=field("company"), title=field("title"),
        contact_email=field("contact_email"),
        job_id=None if job_id is None else int(job_id),
    )


def _key_of(posting: identity.Posting) -> str:
    """The key a posting's attempt is filed under, or a refusal to invent one.

    Discovery asks for a DECISION about a posting that has no row yet, which is
    legitimate. Claiming one without a row is not: the key is what makes "one
    attempt per posting" enforceable, and a made-up key enforces nothing.
    """
    if posting.job_id is None:
        raise ValueError("a posting without an id cannot hold an attempt")
    return key_for_job(posting.job_id)


def decide_for_job(
    con: sqlite3.Connection, job: sqlite3.Row | dict, *, window_days: int | None = None
) -> identity.Decision:
    """What the policy says about this posting right now.

    Read-only, so screens can ask it as freely as gates do — and because both
    ask the same function, a screen cannot promise what the gate will refuse.
    """
    posting = _posting(job)
    decision = identity.decide(
        posting,
        applications(con),
        live_reservations(con),
        window_days=(
            cooldown_days(con) if window_days is None else window_days
        ),
    )
    return _lifted(decision, posting.job_id in authorizations(con))


def decide_for_posting(
    con: sqlite3.Connection,
    *,
    company: str,
    title: str = "",
    contact_email: str = "",
    window_days: int | None = None,
) -> identity.Decision:
    """What the policy says about a posting that is not stored yet.

    Discovery needs this: a posting arriving from a board has no row, and the
    decision determines whether it is filed away permanently or merely held
    back. Asking the same function the gates ask is what keeps a posting from
    being stamped `duplicate` for ever by a rule the rest of the app no longer
    applies.
    """
    return identity.decide(
        identity.Posting(company=company, title=title,
                         contact_email=contact_email),
        applications(con),
        live_reservations(con),
        window_days=(
            cooldown_days(con) if window_days is None else window_days
        ),
    )


def reserve(
    con: sqlite3.Connection,
    job: sqlite3.Row | dict,
    channel: str,
    *,
    override: bool = False,
    override_evidence: str = "",
    now: str,
    window_days: int | None = None,
) -> tuple[bool, identity.Decision]:
    """Claim this posting, or say why it cannot be claimed.

    MUST be called inside the caller's ``BEGIN IMMEDIATE``. The decision and
    the insert have to be one atomic step; splitting them re-opens the very
    window this exists to close.

    `override` is the candidate's recorded "apply anyway" during a cooling-off
    window. It lifts a window and nothing else: a republication is a mistake
    no confirmation makes reasonable, and a live reservation may already be
    leaving.
    """
    decision = decide_for_job(con, job, window_days=window_days)
    if not decision.allowed:
        if not (override and decision.verdict == identity.COOLING_OFF):
            return False, decision

    posting = _posting(job)
    key = _key_of(posting)
    existing = con.execute(
        "SELECT id, state, override_confirmed_at, override_evidence "
        "  FROM application_attempts WHERE idempotency_key=?",
        (key,),
    ).fetchone()
    if existing is not None and existing["state"] != RELEASED:
        # Reserved by a path already running, or recorded by one that
        # finished. Either way this posting's single attempt is taken.
        return False, identity.Decision(
            verdict=(
                identity.RESERVED if existing["state"] == RESERVED
                else identity.BLOCKED_REPUBLICATION
            ),
            reservation_key=key,
        )

    confirmed = now if override and decision.verdict == identity.COOLING_OFF else ""
    evidence = override_evidence if confirmed else ""
    if existing is not None and not confirmed:
        # A standing authorization is what let this reserve happen at all, and
        # it is the evidence ADR 0010 requires the attempt to carry. Reviving
        # the row must not wipe the confirmation that made reviving it legal.
        confirmed = str(existing["override_confirmed_at"] or "")
        evidence = str(existing["override_evidence"] or "")
    if existing is None:
        con.execute(
            """
            INSERT INTO application_attempts (
                idempotency_key, state, company, company_key, position,
                channel, job_id, override_confirmed_at, override_evidence,
                created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (key, RESERVED, posting.company, norm(posting.company),
             posting.title, channel, posting.job_id, confirmed, evidence,
             now, now),
        )
    else:
        # A released attempt is revived rather than replaced. He abandoned a
        # form here and came back; a fresh row would leave the old key taken
        # and the retry refused for ever.
        con.execute(
            """
            UPDATE application_attempts
               SET state=?, company=?, company_key=?, position=?, channel=?,
                   override_confirmed_at=?, override_evidence=?, updated_at=?
             WHERE idempotency_key=?
            """,
            (RESERVED, posting.company, norm(posting.company), posting.title,
             channel, confirmed, evidence, now, key),
        )
    return True, decision


def record(
    con: sqlite3.Connection, key: str, bewerbung_id: int, now: str
) -> None:
    """The attempt produced an application. Only a live reservation may."""
    con.execute(
        "UPDATE application_attempts SET state=?, bewerbung_id=?, updated_at=? "
        " WHERE idempotency_key=? AND state=?",
        (RECORDED, bewerbung_id, now, key, RESERVED),
    )


def release(con: sqlite3.Connection, key: str, now: str) -> None:
    """The attempt ended without an application. The company is free again."""
    con.execute(
        "UPDATE application_attempts SET state=?, updated_at=? "
        " WHERE idempotency_key=? AND state=?",
        (RELEASED, now, key, RESERVED),
    )


def unrecord(con: sqlite3.Connection, bewerbung_id: int, now: str) -> None:
    """The application was taken back out of the ledger, so its attempt was
    never one. Released rather than deleted, and the pointer is cleared
    because `bewerbung_id` is a foreign key into the row about to go."""
    con.execute(
        "UPDATE application_attempts "
        "   SET state=?, bewerbung_id=NULL, updated_at=? "
        " WHERE bewerbung_id=?",
        (RELEASED, now, bewerbung_id),
    )


def reconcile_interrupted(con: sqlite3.Connection, now: str) -> int:
    """Free companies whose reservation outlived the work that held it.

    A reservation is created in the same transaction that puts a draft into
    `sending`, so a draft in `sending` IS the live claim. Anything else
    holding a reservation is the residue of a process that died, and leaving
    it would hide a company for good.

    Evidence-driven rather than clock-driven, like the interrupted-undo
    recovery beside it: a send whose outcome is genuinely unknown keeps its
    draft in `sending`, keeps its reservation, and stays a decision for him —
    which is the existing rule for a stuck claim, not a new one.
    """
    changed = con.execute(
        """
        UPDATE application_attempts
           SET state = 'released', updated_at = ?
         WHERE state = 'reserved'
           AND idempotency_key NOT IN (
                SELECT 'job:' || d.job_id FROM drafts d WHERE d.status = 'sending')
        """,
        (now,),
    )
    return changed.rowcount

def decisions_for_jobs(
    con: sqlite3.Connection, jobs: list, *, window_days: int | None = None
) -> dict[int, identity.Decision]:
    """{job id: what the policy says}, for every posting that is not free.

    Asked per PAGE rather than per row, so one read of the ledger and the
    reservations serves the whole view — the same reason its predecessor was
    written that way, and `jobs.duplicate_of` still cannot answer it: that
    column is written once, when a posting is discovered, so every application
    made afterwards silently makes more rows lie.

    A posting that BECAME an application is not held by it. Without that, every
    row in the "Beworben" view carried a warning about its own application,
    which reads as a duplicate-send error rather than as the record he opened.
    """
    ledger = applications(con)
    reservations = live_reservations(con)
    cleared = authorizations(con)
    days = cooldown_days(con) if window_days is None else window_days
    found: dict[int, identity.Decision] = {}
    for job in jobs:
        keys = job.keys() if isinstance(job, sqlite3.Row) else job
        own = job["bewerbung_id"] if "bewerbung_id" in keys else None
        visible = ([a for a in ledger if a.id != own] if own is not None
                   else ledger)
        decision = _lifted(
            identity.decide(_posting(job), visible, reservations,
                            window_days=days),
            job["id"] in cleared,
        )
        if not decision.allowed:
            found[job["id"]] = decision
    return found


def authorize(
    con: sqlite3.Connection,
    job: sqlite3.Row | dict,
    evidence: str,
    now: str,
) -> tuple[bool, identity.Decision]:
    """Record that the candidate wants to apply here despite the window.

    MUST be called inside the caller's ``BEGIN IMMEDIATE``, like `reserve`:
    the decision it is answering has to be the one still standing when the
    stamp lands.

    Refuses anything the window is not responsible for. A permanent block is
    not the candidate's to overrule, and a live reservation is a message that
    may already be leaving — waiting is the only answer to that one.
    """
    decision = decide_for_job(con, job)
    if decision.verdict != identity.COOLING_OFF:
        return False, decision
    posting = _posting(job)
    key = _key_of(posting)
    existing = con.execute(
        "SELECT id FROM application_attempts WHERE idempotency_key=?", (key,)
    ).fetchone()
    if existing is None:
        con.execute(
            """
            INSERT INTO application_attempts (
                idempotency_key, state, company, company_key, position,
                channel, job_id, override_confirmed_at, override_evidence,
                created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?)
            """,
            (key, RELEASED, posting.company, norm(posting.company),
             posting.title, posting.job_id, now, evidence, now, now),
        )
    else:
        con.execute(
            "UPDATE application_attempts "
            "   SET override_confirmed_at=?, override_evidence=?, updated_at=? "
            " WHERE idempotency_key=?",
            (now, evidence, now, key),
        )
    return True, decision
