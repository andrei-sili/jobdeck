"""The one place a form application is written into the ledger.

Two things will eventually say "this application went out": his own press on
the Läuft strip, and — once replies are read (Phase 3) — an
Eingangsbestätigung arriving from the employer's ATS. They must write the same
row the same way, so they call the same function.

That is not tidiness. The two writers that existed before this disagreed:
`jobs.confirm_applied` passed the Mappe as `dokument` and the cockpit's own
recorder passed nothing, which is why 13 of his 35 Online-Portal ledger rows
point at no document at all while the PDFs sit on disk under output/job_*/.
One recorder is what makes that class of drift impossible rather than merely
fixed once.

What goes into `dokument` is the ARCHIVE, never the staged copy: the file in
`config.UPLOAD_DIR` is a link that is re-made on every build and removed when
the application closes, so recording it would point the ledger at a path that
is about to hold someone else's Bewerbung.
"""

import logging
import pathlib

from jobdeck import attempts, db
from jobdeck.services import upload
from jobdeck.services.mappe import MAPPE_COMPLETE

log = logging.getLogger(__name__)

KANAL_FORM = "Online-Portal"
KANAL_EMAIL = "E-Mail"

# Where he pressed, or what read the reply. Stored on the row's note so the
# ledger can later be asked how many applications the app closed by itself.
SOURCE_HAND = "hand"

# Draft states whose letter is finished and has not gone anywhere else, so an
# application recorded now closes it. Every other state is left alone and says
# why: 'generating' has no letter yet, 'failed' has none at all, 'sent' went by
# e-mail and IS the record of that e-mail, and 'filed'/'discarded' are closed.
FILEABLE = ("ready", "approved")


def _file_letter(con, draft, bewerbung_id: int) -> None:
    """Close the letter against the application that was recorded for it.

    'filed' says only what this app can know: an application for this posting
    is in the ledger, so the letter is no longer waiting to be sent and must
    not be rewritten. It deliberately does NOT claim that an employer read
    this text — the screens derive that from the ledger row's own evidence,
    its channel and its `dokument`, and say plainly when they cannot tell.

    That is what lets one rule serve every hand-recorded channel. The first
    version required a complete Mappe on the form channel, which read well but
    left the headline defect half-fixed: a by-e-mail application recorded by
    hand went on offering its letter in the Postausgang for ever, at a company
    that already had an application — the exact shape this slice exists to
    end. It also made the one-shot migration and this writer disagree about
    the same rows, which is how a state stops meaning one thing.
    """
    if draft is None or draft["status"] not in FILEABLE:
        return
    db.file_draft(con, draft["id"], bewerbung_id)


def record_form_application(job_id: int, source: str = SOURCE_HAND,
                            override: bool = False) -> dict:
    """Write the application for a posting whose form he filled in.

    The named entry point for the form path — the Läuft strip presses it, and
    the Eingangsbestätigung reader will press it too once replies are read.
    """
    return record_application(job_id, KANAL_FORM, source, override=override)


def record_application(job_id: int, kanal: str,
                       source: str = SOURCE_HAND,
                       override: bool = False) -> dict:
    """Write an application he made himself, whichever way it went.

    Returns `{"ok", "bewerbung_id", "company", "duplicate", "decision",
    "undo"}`. The refusing application is handed back as a row rather than as
    a sentence: `ui.helpers` owns that wording so every screen says it the
    same way, and a service that imported the UI to build one string would
    invert the dependency for no gain.

    `override` is the candidate's recorded "apply anyway" from the pile of
    companies currently being left alone. It lifts a cooling-off window and
    nothing else.

    `undo` is False when the gate refused: nothing was written, so there is no
    earlier state an undo could restore and offering one would restore a state
    that never existed.
    """
    with db.db() as con:
        # BEGIN IMMEDIATE, because the decision and the claim have to be one
        # atomic step: two callers interleaving between them both pass a gate
        # meant to admit exactly one. That was theoretical while only a press
        # could record — since replies are read, a background thread records
        # too, and it runs precisely when he is working in the app.
        con.execute("BEGIN IMMEDIATE")
        job = db.get_job(con, job_id)
        if job is None:
            return {"ok": False, "bewerbung_id": None, "company": "",
                    "duplicate": None, "decision": None, "undo": False}
        company = str(job["company"] or "")
        previous_status = str(job["status"] or "new")
        now = attempts.stamp()
        reserved, decision = attempts.reserve(
            con, job, kanal, override=override,
            override_evidence=f"recorded by hand from the held-back pile ({source})",
            now=now,
        )
        if not reserved:
            blocking = (
                db.get_bewerbung(con, decision.application_id)
                if decision.application_id is not None else None
            )
            # Only a PERMANENT refusal files the posting away. A cooling-off
            # window is temporary, and writing it as the status `duplicate`
            # would mean waiting it out never brings the posting back.
            if decision.permanent and blocking is not None:
                db.mark_duplicate_of(con, job_id, blocking["id"])
            con.commit()
            return {"ok": False, "bewerbung_id": None, "company": company,
                    "duplicate": dict(blocking) if blocking is not None else None,
                    "decision": decision, "undo": False}
        draft = db.get_draft_by_job(con, job_id)
        # the archive under output/job_<id>/, not the staged link
        dokument = str((draft["pdf_path"] if draft is not None else "") or "")
        bewerbung_id = db.apply_job(con, job_id, kanal=kanal,
                                    dokument=dokument,
                                    notiz_extra=f"{kanal} ({source})")
        if bewerbung_id is None:
            con.rollback()
            return {"ok": False, "bewerbung_id": None, "company": company,
                    "duplicate": None, "decision": None, "undo": False}
        attempts.record(con, attempts.key_for_job(job_id), bewerbung_id, now)
        # The letter is closed by it: the register can show which text belongs
        # to this company, and — the part that was costing him — the
        # Postausgang stops offering to e-mail a letter whose application is
        # already out.
        _file_letter(con, draft, bewerbung_id)
        # the loop is closed: nothing should still be offered for upload
        upload.clear(job["upload_path"])
        db.set_upload(con, job_id, "", "")
        con.commit()
    log.info("recorded form application for job %s (%s) as bewerbung %s",
             job_id, source, bewerbung_id)
    return {"ok": True, "bewerbung_id": bewerbung_id, "company": company,
            "duplicate": None, "decision": decision, "undo": True,
            "previous_status": previous_status}


def undo(job_id: int, bewerbung_id: int, previous_status: str) -> None:
    """Take the application back out again, or leave it safe to retry.

    A restaged file is prepared before changing the ledger. Database removal
    and its upload pointer then commit together. If either step fails, the
    application remains recorded and any newly staged artifact is removed.
    """
    staged: pathlib.Path | None = None
    with db.db() as con:
        job = db.get_job(con, job_id)
        draft = db.get_draft_by_job(con, job_id)
        archive = str((draft["pdf_path"] if draft is not None else "") or "")
        if job is not None and job["form_opened_at"] and archive:
            source = pathlib.Path(archive)
            if source.is_file():
                staged = upload.stage(
                    source,
                    previous=str(
                        upload.undo_staged_path(source, job_id, bewerbung_id)
                    ),
                )
    try:
        with db.db() as con:
            con.execute("BEGIN IMMEDIATE")
            # BEFORE the ledger row goes: `application_attempts.bewerbung_id`
            # is a foreign key into it, so clearing the pointer afterwards is
            # too late. The attempt was never one, so the company is free.
            attempts.unrecord(con, bewerbung_id, attempts.stamp())
            db.unrecord_application(con, job_id, bewerbung_id, previous_status)
            if staged is not None:
                db.set_upload(con, job_id, str(staged), MAPPE_COMPLETE)
    except Exception:
        if staged is not None:
            upload.clear(staged)
        raise
    log.info("undid the form application for job %s (bewerbung %s)",
             job_id, bewerbung_id)


def abandon_form(job_id: int) -> None:
    """He says no application went out here after all.

    Takes back the start AND the file: a Mappe left in the upload folder for
    an application that was abandoned is the next thing an employer's picker
    offers. The removal has to happen before the pointer is blanked — the same
    statement clears `upload_path`, so afterwards nothing in the app could ever
    find that file again.
    """
    with db.db() as con:
        job = db.get_job(con, job_id)
        if job is None:
            return
        upload.clear(job["upload_path"])
        db.clear_form_opened(con, job_id)
        con.commit()
    log.info("took back the started form application for job %s", job_id)
