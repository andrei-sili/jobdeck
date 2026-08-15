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
import shutil

from jobdeck import config

log = logging.getLogger(__name__)


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


def stage(source: pathlib.Path, previous: str = "") -> pathlib.Path:
    """Put `source` in the upload folder and answer with the staged path.

    Unlinks first: `os.link` refuses an existing destination, and the
    destination existing is the NORMAL case — a rebuilt Mappe re-stages over
    its own previous link.
    """
    target = staged_path(source, previous)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)
    except OSError as exc:
        # another filesystem (EXDEV), or one without hardlinks (exFAT, several
        # network mounts) — the file still has to be there
        log.info("upload: hardlink refused (%s), copying instead",
                 exc.strerror or exc)
        shutil.copyfile(source, target)
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
