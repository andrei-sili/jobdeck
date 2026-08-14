"""The Mappe as a stack of pages, measured rather than described.

An application PDF is only ever built for a posting that already has a
finished Anschreiben, which means the user first sees what an employer
receives at the moment he is about to send it. This module builds the same
thing without a draft — a specimen — so the stack, its weight and every
field the letter head will carry can be looked at, and fixed, beforehand.

Two operations, deliberately apart:

* `inspect` is cheap and runs on every render. It reads the Anlagen from
  disk and the last specimen from its own file, and names anything broken.
* `build` renders the letter through headless Chrome and merges the whole
  Mappe, which takes seconds and therefore only happens when asked for.

The specimen PDF is the cache: page counts and total size are read back from
the artifact itself rather than kept in a second place that could disagree
with it.
"""

import asyncio
import dataclasses
import logging
import pathlib
import tempfile

from jobdeck import apply_channel, config, db, pdf, templates
from jobdeck.services import mappe

log = logging.getLogger(__name__)

_lock = asyncio.Lock()  # one Chrome render at a time, as for a real Mappe

SPECIMEN_NAME = "Bewerbungsmappe_Muster.pdf"

# The letter body an employer never sees. It stands in for the Anschreiben so
# the specimen has the shape of a real letter, and it says what it is in the
# document itself — a specimen that reads like an application is one somebody
# will eventually mistake for one.
SPECIMEN_BODY = (
    "Dies ist eine Musterseite. Hier steht das Anschreiben, das für jede "
    "Bewerbung neu geschrieben wird — dieselbe Vorlage, dieselbe Schrift, "
    "derselbe Seitenumbruch.\n\n"
    "Alles andere auf dieser Seite ist echt: Anschrift, Datum und Betreff "
    "stammen aus der Anzeige und aus deinen Einstellungen. Was hier fehlt, "
    "fehlt auch in der Bewerbung."
)

# The letter template produces the pages an employer reads first. It is one
# document, so its pages are reported as one part — naming them individually
# would mean guessing which page is which from its text.
TEMPLATE_LABEL = "Deckblatt · Anschreiben · Lebenslauf"


@dataclasses.dataclass(frozen=True)
class Part:
    """One source document in the stack, in the order it is merged.

    `size_bytes` is what the part weighs ON DISK, before merging: the built
    Mappe shares repeated images between parts and is then fitted to a
    budget, so the parts do not add up to the total and the screen has to say
    so rather than print numbers that quietly disagree.
    """

    label: str
    pages: int
    size_bytes: int
    first_page: int = 0
    error: str = ""

    @property
    def placed(self) -> bool:
        """Whether this part's position in the stack is known at all."""
        return self.first_page > 0

    @property
    def last_page(self) -> int:
        return self.first_page + self.pages - 1


def specimen_path() -> pathlib.Path:
    return pathlib.Path(config.OUTPUT_DIR) / "muster" / SPECIMEN_NAME


def _int_setting(raw: str) -> int:
    """A byte count out of app_settings, 0 for anything that is not one.

    The directory holding these is one the user is invited to edit, so the
    value is screened rather than trusted to parse."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _numbered(parts: list[Part]) -> list[Part]:
    """Walk the stack and give every part the page it starts on.

    A part whose length is unknown — the letter before the first build, or an
    Anlage that cannot be read — leaves every part AFTER it at an unknown
    offset, and those get `first_page` 0 rather than a number. Numbering
    through the gap was the first thing the running app showed: before a
    build, the Zeugnis was announced as pages 1–2 when the letter takes three
    pages and it really lands on 4–5. A page number is either right or it is
    the wrong kind of wrong for a document somebody prints.
    """
    numbered = []
    page = 1
    for part in parts:
        numbered.append(dataclasses.replace(part, first_page=page))
        if part.pages <= 0:
            page = 0  # nothing after this can be placed any more
        elif page:
            page += part.pages
    return numbered


def anlagen_parts(anlagen_dir: str) -> tuple[list[Part], str]:
    """The Anlagen as they really are on disk, in merge order.

    A part that cannot be read is REPORTED rather than raised past: one
    unreadable certificate must not blank the whole screen, and naming it is
    the point — this is where it can be discovered instead of at send time.
    """
    try:
        files = pdf.collect_anlagen(anlagen_dir)
    except pdf.PdfError as exc:
        return [], str(exc)
    parts = []
    for path in files:
        try:
            pages = pdf.page_count(path)
            error = ""
        except Exception as exc:  # pypdf raises its own family on a torn file
            pages, error = 0, f"nicht lesbar: {exc}"
        parts.append(Part(label=path.stem, pages=pages,
                          size_bytes=path.stat().st_size, error=error))
    return parts, ""


# Written by a build, and read back beside the stack it describes. Numbers
# rather than a sentence: `Compression.describe()` is written for the log and
# is English, and this screen is German. Storing the figures lets the screen
# say it in its own language without a second thing to keep in step.
BEFORE_SETTING = "mappe_specimen_before_bytes"
LOSSLESS_SETTING = "mappe_specimen_lossless"
SPECIMEN_SETTINGS = (BEFORE_SETTING, LOSSLESS_SETTING)


def _folder_fingerprint(anlagen_dir: str) -> tuple:
    """The Anlagen folder as the screen sees it: names, sizes, mtimes.

    No table signature can see a file being added, renamed or replaced, and
    renaming is how the order of the stack is set — so the folder itself has
    to be part of what the page compares. Six stats, on a timer.
    """
    if not (anlagen_dir or "").strip():
        return ()
    folder = pathlib.Path(anlagen_dir).expanduser()
    try:
        entries = sorted(p for p in folder.iterdir()
                         if p.suffix.lower() == ".pdf")
    except OSError:
        return ("unreadable",)
    fingerprint = []
    for path in entries:
        try:
            stat = path.stat()
            fingerprint.append((path.name, stat.st_size, stat.st_mtime_ns))
        except OSError:
            fingerprint.append((path.name, -1, -1))
    return tuple(fingerprint)


def signature(con, job_id: int | None) -> tuple:
    """Everything the Unterlagen screen states, as one comparable tuple."""
    return (
        *db.claims_signature(con),
        *db.profiles_signature(con),
        # profiles_signature cannot see a profile being switched off, and the
        # Suchprofil panel summarises only the active ones.
        db.count_active_profiles(con),
        *(db.get_setting(con, key, "")
          for key in (*mappe.BUILD_SETTING_KEYS, *SPECIMEN_SETTINGS)),
        db.job_signature(con, job_id) if job_id is not None else None,
        _folder_fingerprint(db.get_setting(con, "anlagen_dir", "").strip()),
    )


# What the letter head needs, and how to say it is missing. The reason
# matters more than the fact: "the posting names none" is nothing the user
# can fix, while "set it in Settings" is one click away.
_PREVIEW_FIELDS = (
    ("firma", "Firma", "die Anzeige nennt keine Firma"),
    ("ansprechpartner", "Ansprechpartner",
     "die Anzeige nennt keinen — der Brief beginnt mit „Sehr geehrte Damen "
     "und Herren“"),
    ("strasse", "Straße", "weder Anzeige noch Board nennen eine"),
    ("plz_ort", "PLZ und Ort", "weder Anzeige noch Board nennen sie"),
    ("ort", "Dein Ort", "in den Einstellungen setzen — er trägt die Datumszeile"),
    ("datum", "Datum", ""),
    ("betreff", "Betreff", ""),
)


def preview(con, job_id: int | None) -> dict:
    """The letter head for one posting, and what will be empty in it.

    Same derivation as the real Mappe (`mappe.letter_values`), so a field
    this reports as present cannot be missing in the PDF.
    """
    settings = mappe.build_settings(con)
    job = db.get_job(con, job_id) if job_id is not None else None
    if job is None:
        return {"job": None, "values": {}, "missing": []}
    values = mappe.letter_values(job, None, settings["applicant_name"],
                                 settings["applicant_ort"])
    missing = [
        {"key": key, "label": label, "why": why}
        for key, label, why in _PREVIEW_FIELDS
        if not str(values.get(key, "")).strip()
    ]
    return {"job": dict(job), "values": values, "missing": missing}


def _specimen_facts(parts: list[Part]) -> dict:
    """What the last build produced, read back from the file it produced.

    The letter's page count is the specimen's total minus the Anlagen, which
    is exact: merging preserves both the order and the number of pages.
    """
    path = specimen_path()
    try:
        total_pages = pdf.page_count(path)
        size = path.stat().st_size
    except Exception:
        return {"built": False, "pages": 0, "size_bytes": 0,
                "letter_pages": 0, "built_at": ""}
    anlagen_pages = sum(part.pages for part in parts)
    return {
        "built": True,
        "pages": total_pages,
        "size_bytes": size,
        "letter_pages": max(0, total_pages - anlagen_pages),
        "built_at": path.stat().st_mtime,
    }


def read(con, job_id: int | None) -> dict:
    """The stack, the budgets and the letter head, without rendering anything.

    Takes a connection so the screen can read everything it states — this, the
    register and the search profiles — under one short-lived connection, with
    the signature taken before any of it.
    """
    settings = mappe.build_settings(con)
    shrunk_from = _int_setting(db.get_setting(con, BEFORE_SETTING, ""))
    lossless = db.get_setting(con, LOSSLESS_SETTING, "") == "1"
    view = preview(con, job_id)
    parts, anlagen_error = anlagen_parts(settings["anlagen_dir"])
    facts = _specimen_facts(parts)
    template = pathlib.Path(settings["template_path"]).expanduser() \
        if settings["template_path"] else None
    letter = Part(label=TEMPLATE_LABEL, pages=facts["letter_pages"],
                  size_bytes=0,
                  error="" if template and template.is_file()
                        else "Vorlage fehlt — in den Einstellungen setzen")
    return {
        "settings": settings,
        "parts": _numbered([letter, *parts]),
        "anlagen_error": anlagen_error,
        "specimen": facts,
        "specimen_path": str(specimen_path()),
        "preview": view,
        "target_email_bytes": mappe.target_bytes(settings, ""),
        "target_portal_bytes": mappe.target_bytes(settings,
                                                  apply_channel.CHANNEL_ATS),
        "max_bytes": pdf.MAX_MAPPE_BYTES,
        "shrunk_from_bytes": shrunk_from,
        "lossless": lossless,
    }


def _inspect(job_id: int | None) -> dict:
    with db.db() as con:
        # First, before anything it labels: sqlite3 gives every SELECT its own
        # snapshot, so a write landing between them would marry stale rows to a
        # fresh signature and the watcher would record that as what is showing.
        current = signature(con, job_id)
        return {"signature": current, **read(con, job_id)}


async def inspect(job_id: int | None = None) -> dict:
    """The stack as it stands, on its own connection."""
    return await asyncio.to_thread(_inspect, job_id)


def _build(job_id: int | None) -> dict:
    with db.db() as con:
        settings = mappe.build_settings(con)
        job = db.get_job(con, job_id) if job_id is not None else None
    if not settings["applicant_name"]:
        return {"ok": False, "error": "set your applicant name in Settings first"}
    if not settings["applicant_ort"]:
        return {"ok": False, "error": "set your city (Ort) in Settings first"}
    if not settings["template_path"]:
        return {"ok": False, "error": "set the letter template path in Settings first"}
    template_file = pathlib.Path(settings["template_path"]).expanduser()
    if not template_file.is_file():
        return {"ok": False, "error": f"letter template not found: {template_file}"}
    if job is None:
        return {"ok": False, "error": "no posting to build the specimen from — "
                                      "the letter head needs a real one"}

    values = mappe.letter_values(job, None, settings["applicant_name"],
                                 settings["applicant_ort"])
    values["anschreiben_body"] = SPECIMEN_BODY
    try:
        template_html = template_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "error": f"cannot read the letter template: {exc}"}

    out_path = specimen_path()
    try:
        letter_html = templates.render_letter(template_html, values)
        anlagen = pdf.collect_anlagen(settings["anlagen_dir"])
        # The specimen is fitted to the E-MAIL budget, the gentler of the two:
        # it exists to show what the documents look like, and reporting the
        # harsher portal rung would understate the quality of every send that
        # goes by e-mail.
        budget = mappe.target_bytes(settings, "")
        with tempfile.TemporaryDirectory(prefix="jobdeck_muster_") as tmp:
            letter_pdf = pathlib.Path(tmp) / "anschreiben.pdf"
            pdf.html_to_pdf(letter_html, letter_pdf)
            merged = pathlib.Path(tmp) / "mappe.pdf"
            pdf.merge_pdfs([letter_pdf, *anlagen], merged)
            if settings["compress"] == "1":
                compression = pdf.compress_to_target(merged, out_path, budget)
            else:
                pdf.install_pdf(merged, out_path)
                size = merged.stat().st_size
                compression = pdf.Compression(size_bytes=size,
                                              original_bytes=size,
                                              met_target=size <= budget)
    except (templates.TemplateError, pdf.PdfError) as exc:
        return {"ok": False, "error": str(exc)}

    with db.db() as con:
        db.set_setting(con, BEFORE_SETTING,
                       str(compression.original_bytes if compression.applied
                           else 0))
        db.set_setting(con, LOSSLESS_SETTING,
                       "1" if compression.lossless else "0")
    log.info("specimen Mappe built for job %s: %s pages, %s bytes %s", job_id,
             pdf.page_count(out_path), out_path.stat().st_size,
             compression.describe())
    return {"ok": True, "error": "", "pdf_path": str(out_path),
            "pages": pdf.page_count(out_path),
            "size_bytes": out_path.stat().st_size,
            "compression": compression.describe(),
            "met_target": compression.met_target}


async def build(job_id: int | None = None) -> dict:
    """Render the specimen Mappe. Seconds, headless Chrome, no LLM."""
    async with _lock:
        return await asyncio.to_thread(_build, job_id)
