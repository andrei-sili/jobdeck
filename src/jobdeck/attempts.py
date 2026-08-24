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

import sqlite3

from jobdeck import identity, settings
from jobdeck.dedupe import norm

# States a row can hold. `released` is kept rather than deleted: "he started
# here and stopped" is evidence, and a deleted row cannot explain why a key
# was once taken.
RESERVED = "reserved"
RECORDED = "recorded"
RELEASED = "released"

COOLDOWN_SETTING = "company_cooldown_days"
DEFAULT_COOLDOWN_DAYS = 60

# The ledger stays the source of truth for "did an application go out", and
# the attempt table only supplies the position it never stored. Read this way
# round, dropping the new table degrades the gate to today's behaviour rather
# than opening it.
_APPLICATIONS_SQL = """
SELECT b.id                            AS id,
       COALESCE(b.firma, '')           AS company,
       COALESCE(b.email, '')           AS email,
       COALESCE(b.gesendet_am, '')     AS sent_on,
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
        )
        for row in con.execute(_APPLICATIONS_SQL)
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
    if job_id is None:
        raise ValueError("a posting without an id cannot hold an attempt")
    return identity.Posting(
        company=field("company"), title=field("title"),
        contact_email=field("contact_email"), job_id=int(job_id),
    )


def decide_for_job(
    con: sqlite3.Connection, job: sqlite3.Row | dict, *, window_days: int | None = None
) -> identity.Decision:
    """What the policy says about this posting right now.

    Read-only, so screens can ask it as freely as gates do — and because both
    ask the same function, a screen cannot promise what the gate will refuse.
    """
    return identity.decide(
        _posting(job),
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
    window. It can lift a window and nothing else: a republication and a live
    reservation are not his to overrule — one is a mistake he cannot want, the
    other is a send already in flight.
    """
    decision = decide_for_job(con, job, window_days=window_days)
    if not decision.allowed:
        if not (override and decision.verdict == identity.COOLING_OFF):
            return False, decision

    posting = _posting(job)
    key = key_for_job(posting.job_id)
    existing = con.execute(
        "SELECT id, state FROM application_attempts WHERE idempotency_key=?",
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
