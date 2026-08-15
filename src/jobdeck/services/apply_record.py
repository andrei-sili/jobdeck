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

from jobdeck import db
from jobdeck.services import upload
from jobdeck.services.mappe import MAPPE_COMPLETE

log = logging.getLogger(__name__)

KANAL_FORM = "Online-Portal"
KANAL_EMAIL = "E-Mail"

# Where he pressed, or what read the reply. Stored on the row's note so the
# ledger can later be asked how many applications the app closed by itself.
SOURCE_HAND = "hand"


def record_form_application(job_id: int, source: str = SOURCE_HAND) -> dict:
    """Write the application for a posting whose form he filled in.

    The named entry point for the form path — the Läuft strip presses it, and
    the Eingangsbestätigung reader will press it too once replies are read.
    """
    return record_application(job_id, KANAL_FORM, source)


def record_application(job_id: int, kanal: str,
                       source: str = SOURCE_HAND) -> dict:
    """Write an application he made himself, whichever way it went.

    Returns `{"ok", "bewerbung_id", "company", "duplicate", "undo"}`. The
    refusing application is handed back as a row rather than as a sentence:
    `ui.helpers.applied_line` owns that wording so every screen says it the
    same way, and a service that imported the UI to build one string would
    invert the dependency for no gain.

    `undo` is False when the duplicate gate refused: `db.apply_job` has ALREADY
    marked the posting a duplicate and pointed it at the existing application
    before returning None, so there is no earlier state an undo could restore
    and offering one would restore a state that never existed.
    """
    with db.db() as con:
        job = db.get_job(con, job_id)
        if job is None:
            return {"ok": False, "bewerbung_id": None, "company": "",
                    "duplicate": None, "undo": False}
        company = str(job["company"] or "")
        previous_status = str(job["status"] or "new")
        draft = db.get_draft_by_job(con, job_id)
        # the archive under output/job_<id>/, not the staged link
        dokument = str((draft["pdf_path"] if draft is not None else "") or "")
        bewerbung_id = db.apply_job(con, job_id, kanal=kanal,
                                    dokument=dokument,
                                    notiz_extra=f"{kanal} ({source})")
        if bewerbung_id is None:
            dup = db.find_duplicate_bewerbung(con, company, job["contact_email"])
            con.commit()   # apply_job already marked the posting a duplicate
            return {"ok": False, "bewerbung_id": None, "company": company,
                    "duplicate": dict(dup) if dup is not None else None,
                    "undo": False}
        # the loop is closed: nothing should still be offered for upload
        upload.clear(job["upload_path"])
        db.set_upload(con, job_id, "", "")
        con.commit()
    log.info("recorded form application for job %s (%s) as bewerbung %s",
             job_id, source, bewerbung_id)
    return {"ok": True, "bewerbung_id": bewerbung_id, "company": company,
            "duplicate": None, "undo": True,
            "previous_status": previous_status}


def undo(job_id: int, bewerbung_id: int, previous_status: str) -> None:
    """Take the application back out again — every write, or none.

    Including the two the ledger does not hold. Recording clears the staged
    file and blanks `upload_path`/`mappe_kind`, so an undo that reversed only
    `apply_job` handed him back an application whose strip entry read "Mappe
    NICHT fertig" while the complete Mappe sat untouched at `drafts.pdf_path`
    — and whose "Ordner öffnen" had disappeared.
    """
    with db.db() as con:
        db.unrecord_application(con, job_id, bewerbung_id, previous_status)
        job = db.get_job(con, job_id)
        draft = db.get_draft_by_job(con, job_id)
        archive = str((draft["pdf_path"] if draft is not None else "") or "")
        if job is not None and job["form_opened_at"] and archive:
            source = pathlib.Path(archive)
            if source.is_file():
                staged = upload.stage(source)
                db.set_upload(con, job_id, str(staged), MAPPE_COMPLETE)
        con.commit()
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
