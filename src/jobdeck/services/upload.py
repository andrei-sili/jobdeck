"""One folder an employer's file picker ever opens in.

His complaint was "when the form asks for the file, it opens the PREVIOUS
application's folder". That is not a wrong folder — it is the absence of one.
The Mappe is archived under ``output/job_<id>/``, a new directory per
application, and every file chooser on the platform reopens wherever it was
last used. So the picker was structurally guaranteed to be one application
behind, every single time.

`config.UPLOAD_DIR` is the fix: one path, forever, holding one file per
application in flight. The archive stays exactly where it is — it is what
`bewerbungen.dokument` points at and what he keeps — and what lives here is a
HARDLINK to it, so a staged file costs no bytes and cannot drift from the
artifact it names.

Two properties this module exists to hold:

* **Re-staged after every build.** `pdf.install_pdf` and `pdf._write_deduplicated`
  both end in `replace()`, which gives the destination a NEW inode. A link made
  before or during a build points at the old bytes forever — it opens fine,
  looks complete, and is the previous letter.
* **Copy fallback.** A hardlink needs one filesystem. `DATA_DIR` is
  env-overridable, so it may sit on another mount (EXDEV) or on exFAT and
  several network mounts (ENOTSUP). Falling back is not defensive tidiness;
  without it the feature simply fails for anyone who moved their data dir.
"""

import logging
import os
import pathlib
import re
import shutil
import tempfile

from jobdeck import config, db

log = logging.getLogger(__name__)

_UNDO_NAME = re.compile(r"^.+ \(restore-(\d+)-(\d+)\)(?:\.[^.]*)?$")


def staged_path(source: pathlib.Path, previous: str = "") -> pathlib.Path:
    """Where `source` is offered to an employer's form.

    The artifact's own name is used, which already carries the company
    (``Bewerbung_<Name>_<Firma>.pdf``): the folder holds several applications
    at once — he opened six forms in thirteen minutes on the day this was
    measured — and one press-ready name per company is what tells them apart
    in a file dialog that shows nothing else about them.

    But that name is NOT unique, and the folder is flat. `pdf.safe_filename`
    collapses every non-alphanumeric run and truncates, so "Müller & Co. KG"
    and "Mueller Co KG" produce one name; two postings at one company produce
    one name by construction (his corpus has an employer with 27 of them); and
    a posting with no company falls back to the literal "Initiativ". Under the
    old per-job folders that could not happen. So a name already taken by a
    DIFFERENT application is disambiguated — the second application gets
    "… (2).pdf" — while `previous` lets a rebuild land on its own file again
    instead of walking one further along every time.
    """
    folder = pathlib.Path(config.UPLOAD_DIR)
    if previous:
        mine = pathlib.Path(previous)
        if mine.parent == folder:
            return mine
    target = folder / source.name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for n in range(2, 100):
        candidate = folder / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
    # 98 applications in flight at one company name is not a real state; a
    # deterministic last resort beats raising inside a finished build.
    return folder / f"{stem} (99){suffix}"


def undo_staged_path(
    source: pathlib.Path, job_id: int, bewerbung_id: int
) -> pathlib.Path:
    """Return the stable target used by one retryable application undo."""
    return pathlib.Path(config.UPLOAD_DIR) / (
        f"{source.stem} (restore-{job_id}-{bewerbung_id}){source.suffix}"
    )


def recover_interrupted_undos(con) -> int:
    """Remove staging left by an undo that crashed before its DB commit.

    A completed undo deleted the application row and left the job pointing at
    the deterministic target, so it is retained. An interrupted undo still has
    the application linked to the job and its target is safe to remove. Partial
    siblings can only come from a stage operation interrupted before replace;
    startup runs before workers, so all of them are stale.
    """
    folder = pathlib.Path(config.UPLOAD_DIR)
    if not folder.is_dir():
        return 0
    removed = 0
    for partial in folder.glob(".*.part"):
        try:
            partial.unlink()
        except OSError:
            log.warning("upload: could not remove interrupted stage %s", partial)
        else:
            removed += 1
    for target in folder.iterdir():
        match = _UNDO_NAME.fullmatch(target.name)
        if match is None or not target.is_file():
            continue
        job_id, bewerbung_id = (int(value) for value in match.groups())
        application_still_recorded = con.execute(
            "SELECT 1 FROM jobs j JOIN bewerbungen b ON b.id=j.bewerbung_id "
            "WHERE j.id=? AND b.id=?",
            (job_id, bewerbung_id),
        ).fetchone()
        if application_still_recorded is None:
            continue
        try:
            target.unlink()
        except OSError:
            log.warning("upload: could not remove interrupted undo %s", target)
        else:
            removed += 1
    return removed


def sweep_orphans(con) -> list[str]:
    """Remove every file in the upload folder that no live row offers.

    A staged file is meaningful only while an application is UNDER WAY and
    a job's `upload_path` or a document row's `staged_path` names it — that
    is what the strip shows and what the recorder takes back. Anything else
    in the folder is a leftover the picker will offer for the NEXT
    application: on 2026-09-04 the real folder held 21 Mappen from August
    applications recorded before staging was taken back on record — 20 of
    them still named by the `upload_path` of a job long since `applied`,
    which is why the job's status is part of the rule, the same way the
    strip's own query (`_STARTED_FORM_SQL`) reads it. Startup sweeps by that
    rule rather than by hand; `.part` files belong to
    `recover_interrupted_undos`.

    Answers with the names removed, for the log."""
    folder = pathlib.Path(config.UPLOAD_DIR)
    if not folder.is_dir():
        return []
    # Exactly the strip's own definition of "under way" (`_STARTED_FORM_SQL`):
    # a form he opened, on a posting not yet closed. Eight more leftovers had
    # a job still `new` with a blank form_opened_at — staged by an older
    # flow for a form never opened, shown on no strip, offered by the picker.
    live = ("j.form_opened_at <> '' "
            "AND j.status NOT IN ('applied','duplicate','skipped')")
    referenced = {row[0] for row in con.execute(
        f"SELECT j.upload_path FROM jobs j WHERE j.upload_path <> '' AND {live}")}
    referenced |= {row[0] for row in con.execute(
        "SELECT d.staged_path FROM application_documents d JOIN jobs j ON j.id = d.job_id "
        f"WHERE d.staged_path <> '' AND {live}")}
    referenced = {str(pathlib.Path(p)) for p in referenced}
    removed = []
    for target in sorted(folder.iterdir()):
        if not target.is_file() or target.name.startswith("."):
            continue
        if str(target) in referenced:
            continue
        try:
            target.unlink()
        except OSError:
            log.warning("upload: could not remove orphan %s", target)
        else:
            removed.append(target.name)
    return removed


def stage(source: pathlib.Path, previous: str = "") -> pathlib.Path:
    """Put `source` in the upload folder and answer with the staged path.

    Build beside the current target and replace it only after the link or copy
    is complete. A failed fallback copy therefore leaves neither a partial
    final file nor a missing previously valid staged file.
    """
    target = staged_path(source, previous)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".part"
    )
    os.close(fd)
    temporary = pathlib.Path(temporary_name)
    try:
        temporary.unlink()
        try:
            os.link(source, temporary)
        except OSError as exc:
            # another filesystem (EXDEV), or one without hardlinks (exFAT,
            # several network mounts) — the file still has to be there
            log.info("upload: hardlink refused (%s), copying instead",
                     exc.strerror or exc)
            shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def clear(path: str | pathlib.Path) -> None:
    """Take a staged file back out of the upload folder.

    Called when an application closes — either way — and when a build fails.
    A file left behind for an application that is finished or abandoned is the
    next thing the picker offers for the next one, which is the defect this
    whole folder exists to end.

    Refuses to touch anything outside the folder: the path comes from a
    database column, and this function deletes.
    """
    if not path:
        return
    target = pathlib.Path(path)
    folder = pathlib.Path(config.UPLOAD_DIR)
    try:
        inside = target.resolve().parent == folder.resolve()
    except OSError:
        return
    if not inside:
        log.warning("upload: refusing to remove %s — outside %s", target, folder)
        return
    target.unlink(missing_ok=True)


def withdraw(con, job_id: int) -> None:
    """Take a posting's whole package out of circulation: every staged file
    out of the folder, the document rows gone, the job's own pointer blank.

    Called when the letter the package was built from stops being the
    letter — a redraft, an edit in the editor, a build discarded mid-render.
    Without it the strip kept printing "Mappe bereit · auch einzeln" and
    offering a "⧉ Anschreiben" that carried the text he had just replaced.
    The archive files under output/job_<id>/ are left alone; the next build
    overwrites them."""
    job = db.get_job(con, job_id)
    if job is not None:
        clear(job["upload_path"])
    for row in db.list_documents(con, job_id):
        clear(row["staged_path"])
    db.clear_documents(con, job_id)
    db.set_upload(con, job_id, "", "")
