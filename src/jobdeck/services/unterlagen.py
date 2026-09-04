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
import os
import pathlib
import tempfile

from jobdeck import apply_channel, config, db, pdf, templates
from jobdeck import settings as app_settings
from jobdeck.services import anlagen as anlagen_lib
from jobdeck.services import atscheck, mappe

log = logging.getLogger(__name__)

_lock = asyncio.Lock()  # one Chrome render at a time, as for a real Mappe

SPECIMEN_NAME = "Bewerbungsmappe_Muster.pdf"
# The one-column Lebenslauf for portals, rendered once so the ATS check can
# measure the file a portal will actually parse rather than its HTML.
SPECIMEN_CV_NAME = "Lebenslauf_Muster.pdf"

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
    # The file this part IS, for the rows that have one. Empty for the letter,
    # which comes out of the template and has no file in the Anlagen folder —
    # and that emptiness is what decides whether a row offers to move or
    # remove itself, rather than the row's position in the list.
    name: str = ""

    @property
    def placed(self) -> bool:
        """Whether this part's position in the stack is known at all."""
        return self.first_page > 0

    @property
    def last_page(self) -> int:
        return self.first_page + self.pages - 1


def specimen_cv_path() -> pathlib.Path:
    return pathlib.Path(config.OUTPUT_DIR) / "muster" / SPECIMEN_CV_NAME


def specimen_path() -> pathlib.Path:
    return pathlib.Path(config.OUTPUT_DIR) / "muster" / SPECIMEN_NAME


def _int_setting(raw: str) -> int:
    """A byte count out of app_settings, 0 for anything that is not one.

    The directory holding these is one the user is invited to edit, so the
    value is screened rather than trusted to parse."""
    return app_settings.parse_int(raw, 0, minimum=0)


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
    except (pdf.PdfError, OSError) as exc:
        # Reported, never raised past: one unreadable folder must not blank the
        # screen that is the only place the problem can be seen.
        return [], str(exc)
    parts = []
    for path in files:
        try:
            pages = pdf.page_count(path)
            error = ""
        except Exception as exc:  # pypdf raises its own family on a torn file
            pages, error = 0, f"nicht lesbar: {exc}"
        parts.append(Part(label=path.stem, pages=pages,
                          size_bytes=path.stat().st_size, error=error,
                          name=path.name))
    return parts, ""


# Written by a build, and read back beside the stack it describes. Numbers
# rather than a sentence: `Compression.describe()` is written for the log and
# is English, and this screen is German. Storing the figures lets the screen
# say it in its own language without a second thing to keep in step.
BEFORE_SETTING = "mappe_specimen_before_bytes"
LOSSLESS_SETTING = "mappe_specimen_lossless"
# What the build MEASURED, so the screen never has to infer it back out of the
# artifact: the letter's own length, and the Anlagen that were merged with it.
LETTER_PAGES_SETTING = "mappe_specimen_letter_pages"
ANLAGEN_SETTING = "mappe_specimen_anlagen"
SPECIMEN_SETTINGS = (BEFORE_SETTING, LOSSLESS_SETTING, LETTER_PAGES_SETTING,
                     ANLAGEN_SETTING)


def _folder_fingerprint(anlagen_dir: str) -> tuple:
    """The Anlagen folder as the screen sees it: names, sizes, mtimes.

    No table signature can see a file being added, renamed or replaced, and
    renaming is how the order of the stack is set — so the folder itself has
    to be part of what the page compares. Six stats, on a timer.
    """
    folder = config.user_path(anlagen_dir)
    if folder is None:
        return ()
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


# app_settings this screen PRINTS beyond the ones the Mappe is built from.
# `stale_age_days` is not decoration here: it is the age filter that decides
# which posting the whole letter-head panel is built from.
WATCHED_SETTINGS = ("global_hard_tags", "stale_age_days")


def _file_fingerprint(path: pathlib.Path | None) -> tuple:
    """One file as the screen sees it: whether it is there, and what it is.

    Existence alone is not enough for either of the two files this covers. The
    template decides the letter's page count, so editing it changes every page
    span the stack prints; the specimen IS the screen's cache of the built
    Mappe, so replacing or deleting it changes the total, the weight and
    whether "Ansehen" leads anywhere.
    """
    if path is None:
        return ()
    try:
        stat = path.stat()
    except OSError:
        return ("missing",)
    # Readability, not only existence: a file whose mode stops it being read
    # stats perfectly, so the screen would keep reporting what it could not
    # read — and getting the mode back would change nothing it compares.
    return (stat.st_size, stat.st_mtime_ns, os.access(path, os.R_OK))


def signature(con, job_id: int | None) -> tuple:
    """Everything the Unterlagen screen states, as one comparable tuple."""
    template = db.get_setting(con, "template_path", "").strip()
    return (
        *db.claims_signature(con),
        *db.profiles_signature(con),
        # profiles_signature cannot see a profile being switched off, and the
        # Suchprofil panel summarises only the active ones.
        db.count_active_profiles(con),
        *(db.get_setting(con, key, "")
          for key in (*mappe.BUILD_SETTING_KEYS, *SPECIMEN_SETTINGS,
                      *WATCHED_SETTINGS)),
        db.job_signature(con, job_id) if job_id is not None else None,
        _folder_fingerprint(db.get_setting(con, "anlagen_dir", "").strip()),
        # The PATHS to these two are settings and already above; their
        # CONTENTS are facts on disk that no table and no setting can see.
        _file_fingerprint(config.user_path(template)),
        _file_fingerprint(specimen_path()),
        _file_fingerprint(config.user_path(
            db.get_setting(con, "cv_ats_path", "").strip())),
        _file_fingerprint(specimen_cv_path()),
        # The same reasoning, for the file the coverage line measures: he
        # edits profile.md outside this app, and the screen would go on
        # naming sections he had renamed or filled.
        _file_fingerprint(config.PROFILE_PATH),
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

# The address block has a THIRD outcome, and it is the only one the user can
# do something about: under Arbeitnehmerüberlassung `templates.letter_address`
# refuses the board's work address on purpose, because it belongs to a client
# and not to the employer the letter is addressed to. Reporting that as "no
# address anywhere" sends him to the ad, where he finds one, and concludes the
# extraction is broken.
_TEMP_AGENCY_REASON = (
    "Arbeitnehmerüberlassung — das Board nennt zwar eine Adresse, aber das "
    "ist der Einsatzort beim Kunden, nicht der Empfänger des Briefes"
)
_ADDRESS_KEYS = ("strasse", "plz_ort")


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
    temp_agency = bool(job["temp_agency"])
    missing = [
        {"key": key, "label": label,
         "why": (_TEMP_AGENCY_REASON
                 if temp_agency and key in _ADDRESS_KEYS else why)}
        for key, label, why in _PREVIEW_FIELDS
        if not str(values.get(key, "")).strip()
    ]
    return {"job": dict(job), "values": values, "missing": missing}


def _specimen_facts(con, parts: list[Part]) -> dict:
    """What the last build produced — the letter's own length MEASURED, and
    whether the Anlagen have moved under it since.

    The letter's page count used to be the specimen's total minus the Anlagen
    as they are on disk NOW. That is a subtraction between two different
    moments, and every page number on the screen rode on it: dropping one new
    certificate into the folder silently re-attributed its pages to the letter
    and shifted every range below, while the total still read like a fresh
    measurement. An unreadable folder attributed the WHOLE Mappe to the letter.

    So the build writes down what it actually saw — the letter's pages, and a
    fingerprint of the Anlagen it merged — and this reads those back. The page
    numbers are then correct even after the folder changes, because only the
    letter's length is remembered and the Anlagen are always counted fresh; the
    total SIZE is the one figure that goes stale, and it is flagged rather than
    reprinted as if it still held.
    """
    path = specimen_path()
    try:
        size = path.stat().st_size
        total_pages = pdf.page_count(path)
    except Exception:
        return {"built": False, "pages": 0, "size_bytes": 0,
                "letter_pages": 0, "stale": False, "built_pages": 0}
    letter_pages = _int_setting(db.get_setting(con, LETTER_PAGES_SETTING, ""))
    built_with = db.get_setting(con, ANLAGEN_SETTING, "")
    stale = built_with != _anlagen_stamp(parts)
    return {
        "built": True,
        # What the stack really is now: the remembered letter plus the Anlagen
        # as they stand. Reading the total off the old artifact is exactly the
        # figure that was wrong.
        "pages": letter_pages + sum(part.pages for part in parts),
        "built_pages": total_pages,
        "size_bytes": size,
        "letter_pages": letter_pages,
        "stale": stale,
    }


def _anlagen_stamp(parts: list[Part]) -> str:
    """The Anlagen a build saw, in a form two builds can be compared by.

    Names and page counts rather than mtimes: re-saving a certificate without
    changing it should not tell the user his measurements have gone stale, and
    what the stack STATES about an Anlage is its name and its length.
    """
    return "|".join(f"{part.label}:{part.pages}" for part in parts)


# What the Anlagen folder is, as four states that need four different
# answers. "No Anlagen" was one state before, and it read the same whether he
# had never chosen a folder, had moved it, or had simply not put anything in
# it yet — while the stack below it drew a perfectly plausible Mappe made of
# the letter alone.
def folder_state(anlagen_dir: str, count: int) -> dict:
    """(state, path, note) for the folder the Anlagen are merged from."""
    folder = anlagen_lib.resolve(anlagen_dir)
    if folder is None:
        text = (anlagen_dir or "").strip()
        if text:
            # A path the system cannot even expand — "~name" with no such
            # user. Saying "no folder chosen" would send him to set one he
            # believes he already set.
            return {"state": "missing", "path": text, "note":
                    f"Mit diesem Pfad kann nichts angefangen werden: {text} — "
                    f"in den Einstellungen korrigieren."}
        return {"state": "unset", "path": "", "note":
                "Noch kein Ordner für deine Anlagen — Zeugnisse und "
                "Zertifikate haben hier keinen Platz, und die Mappe besteht "
                "nur aus dem Brief."}
    if not folder.is_dir():
        return {"state": "missing", "path": str(folder), "note":
                f"Diesen Ordner gibt es nicht: {folder} — er wurde "
                f"verschoben, oder der Pfad in den Einstellungen stimmt "
                f"nicht."}
    if not anlagen_lib.readable(folder):
        # Mounted and not readable answers exactly like empty, and a Mappe
        # built from it would be the letter alone — reported as complete.
        return {"state": "unreadable", "path": str(folder), "note":
                f"Der Ordner lässt sich gerade nicht lesen: {folder} — ist "
                f"das Laufwerk eingebunden?"}
    if not count:
        return {"state": "empty", "path": str(folder), "note":
                "Der Ordner ist leer — die Mappe bestünde nur aus dem "
                "Brief, ohne ein einziges Zeugnis."}
    return {"state": "ok", "path": str(folder), "note": ""}


def read(con, job_id: int | None) -> dict:
    """The stack, the budgets and the letter head, without rendering anything.

    Takes a connection so the screen can read everything it states — this, the
    register and the search profiles — under one short-lived connection, with
    the signature taken before any of it.
    """
    settings = mappe.build_settings(con)
    shrunk_from = _int_setting(db.get_setting(con, BEFORE_SETTING, ""))
    lossless = app_settings.boolean(con, LOSSLESS_SETTING, False)
    view = preview(con, job_id)
    parts, anlagen_error = anlagen_parts(settings["anlagen_dir"])
    facts = _specimen_facts(con, parts)
    template = config.user_path(settings["template_path"])
    letter = Part(label=TEMPLATE_LABEL, pages=facts["letter_pages"],
                  size_bytes=0,
                  error="" if template and template.is_file()
                        else "Vorlage fehlt — in den Einstellungen setzen")
    return {
        "settings": settings,
        "parts": _numbered([letter, *parts]),
        "anlagen_error": anlagen_error,
        "folder": folder_state(settings["anlagen_dir"], len(parts)),
        "specimen": facts,
        "specimen_path": str(specimen_path()),
        "preview": view,
        "target_email_bytes": mappe.target_bytes(settings, ""),
        "target_portal_bytes": mappe.target_bytes(settings,
                                                  apply_channel.CHANNEL_ATS),
        "max_bytes": pdf.MAX_MAPPE_BYTES,
        "shrunk_from_bytes": shrunk_from,
        "lossless": lossless,
        # A budget line may only promise more compression when compression is
        # actually switched on; with it off, what was merged is what is sent.
        "compress": settings["compress"],
        "ats": ats_reports(settings, facts["letter_pages"]),
    }


def ats_reports(settings: dict, letter_pages: int = 0) -> dict:
    """What a portal's parser will make of the two files it can be given:
    the specimen Mappe (the whole package, uploaded as one) and the specimen
    one-column Lebenslauf. None where the file has not been built.

    Measured on the built PDFs, never on the HTML: the properties a parser
    trips over (Type 3 fonts, lost spaces, letter-spaced headings) only
    exist in the rendered file."""
    portal_budget = mappe.target_bytes(settings, apply_channel.CHANNEL_ATS)
    reports: dict = {"mappe": None, "lebenslauf": None,
                     "cv_configured": bool(settings["cv_ats_path"])}
    if specimen_path().is_file():
        # Text and fonts are judged on the template's pages only — the
        # Anlagen behind them are scans, and a scan's OCR layer is not the
        # CV's typography. `letter_pages` is what the last build measured;
        # 0 before any build, which means the whole file.
        reports["mappe"] = atscheck.inspect(specimen_path(),
                                            budget_bytes=portal_budget,
                                            first_pages=letter_pages)
    if settings["cv_ats_path"] and specimen_cv_path().is_file():
        reports["lebenslauf"] = atscheck.inspect(specimen_cv_path(),
                                                 budget_bytes=portal_budget)
    return reports


# ---------------------------------------------------------------------------
# What the spine says about this rubric.
#
# It used to say how many SEARCH PROFILES he has — under the heading
# "Unterlagen", on the app whose complaint was that nothing was where he
# expected it. The documents are three things, and a Mappe an employer can
# receive needs all three: the letter template, at least one Anlage, and one
# build to have measured them.
#
# Deliberately cheap: names, sizes and mtimes only. Counting PAGES would mean
# opening every certificate on every page load and every thirty-second tick,
# and the page counts already have a home on the screen that states them.
# ---------------------------------------------------------------------------
RAIL_PARTS = 3


def rail_facts(con) -> dict:
    """(documents, what is missing, whether a build has measured it)."""
    settings = mappe.build_settings(con)
    template = settings["template_path"].strip()
    resolved = config.user_path(template)
    template_ok = resolved is not None and resolved.is_file()
    entries = anlagen_lib.listing(anlagen_lib.resolve(settings["anlagen_dir"]))
    state = folder_state(settings["anlagen_dir"], len(entries))
    # A specimen file alone is not a measurement: the letter's own length is
    # what a build writes down, and without it every page span on the screen
    # is unknown.
    built = bool(specimen_path().is_file()
                 and _int_setting(db.get_setting(con, LETTER_PAGES_SETTING, "")))
    return {"template_ok": template_ok, "anlagen": len(entries),
            "folder_state": state["state"], "built": built,
            "documents": (1 if template_ok else 0) + len(entries)}


def rail_fingerprint(con) -> tuple:
    """The same facts as one comparable value, for the spine's signature.

    None of it is in a table, so without this the rail would go on reporting
    an empty Anlagen folder for the life of the page he just filled — which is
    the exact staleness class the live watcher exists to end.
    """
    settings = mappe.build_settings(con)
    template = settings["template_path"].strip()
    return (
        # The PATHS themselves, not only what is at them: a folder just
        # created and still empty fingerprints identically to no folder at
        # all, so pressing "Ordner anlegen und verwenden" would leave the
        # spine reading "kein Ordner für Anlagen" for the life of the page.
        settings["anlagen_dir"],
        template,
        _folder_fingerprint(settings["anlagen_dir"]),
        _file_fingerprint(config.user_path(template)),
        _file_fingerprint(specimen_path()),
        _file_fingerprint(config.user_path(
            db.get_setting(con, "cv_ats_path", "").strip())),
        _file_fingerprint(specimen_cv_path()),
        db.get_setting(con, LETTER_PAGES_SETTING, ""),
    )


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
    # German, like everything else the screen shows him. These refusals are
    # the first thing a fresh install meets, and the toast that carries them
    # sits between two German sentences.
    if not settings["applicant_name"]:
        return {"ok": False, "error": "Trage zuerst deinen Namen in den "
                                      "Einstellungen ein"}
    if not settings["applicant_ort"]:
        return {"ok": False, "error": "Trage zuerst deinen Ort in den "
                                      "Einstellungen ein — er trägt die "
                                      "Datumszeile"}
    if not settings["template_path"]:
        return {"ok": False, "error": "Trage zuerst den Pfad zur Briefvorlage "
                                      "in den Einstellungen ein"}
    template_file = config.user_path(settings["template_path"])
    if not template_file.is_file():
        return {"ok": False, "error": f"Briefvorlage nicht gefunden: "
                                      f"{template_file}"}
    if job is None:
        return {"ok": False, "error": "Keine Anzeige, aus der gebaut werden "
                                      "könnte — der Briefkopf braucht eine "
                                      "echte"}

    values = mappe.letter_values(job, None, settings["applicant_name"],
                                 settings["applicant_ort"])
    values["anschreiben_body"] = SPECIMEN_BODY
    try:
        template_html = template_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "error": f"Briefvorlage nicht lesbar: {exc}"}

    out_path = specimen_path()
    letter_pages = 0
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
            # Measured here, while the two documents are still apart. Deriving
            # it later by subtracting the Anlagen from the merged total looks
            # equivalent and is not: the Anlagen can change afterwards, and
            # then the subtraction quietly re-attributes their pages.
            letter_pages = pdf.page_count(letter_pdf)
            merged = pathlib.Path(tmp) / "mappe.pdf"
            pdf.merge_pdfs([letter_pdf, *anlagen], merged)
            if settings["compress"]:
                compression = pdf.compress_to_target(merged, out_path, budget)
            else:
                pdf.install_pdf(merged, out_path)
                size = merged.stat().st_size
                compression = pdf.Compression(size_bytes=size,
                                              original_bytes=size,
                                              met_target=size <= budget)
    except (templates.TemplateError, pdf.PdfError) as exc:
        return {"ok": False, "error": str(exc)}

    cv_error = _build_cv_specimen(settings)
    total_pages = pdf.page_count(out_path)
    size_bytes = out_path.stat().st_size
    with db.db() as con:
        db.set_setting(con, BEFORE_SETTING,
                       str(compression.original_bytes if compression.applied
                           else 0))
        db.set_setting(con, LOSSLESS_SETTING,
                       "1" if compression.lossless else "0")
        # What this build saw, so the screen never infers it back out of the
        # artifact: the letter's own length, and the Anlagen it was merged
        # with — which is how a later change to the folder becomes visible
        # instead of silently rewriting every page number.
        db.set_setting(con, LETTER_PAGES_SETTING, str(letter_pages))
        db.set_setting(con, ANLAGEN_SETTING, _anlagen_stamp(
            anlagen_parts(settings["anlagen_dir"])[0]))
    log.info("specimen Mappe built for job %s: %s pages (%s letter), %s bytes %s",
             job_id, total_pages, letter_pages, size_bytes,
             compression.describe())
    return {"ok": True, "error": "", "pdf_path": str(out_path),
            "pages": total_pages,
            "letter_pages": letter_pages,
            "size_bytes": size_bytes,
            "compression": compression.describe(),
            "met_target": compression.met_target,
            "cv_error": cv_error}


def _build_cv_specimen(settings: dict) -> str:
    """Render the portal Lebenslauf beside the specimen Mappe, '' on success
    or when none is configured. A failure here is reported, not raised: the
    Mappe was built, and the ATS panel says what is missing."""
    if not settings["cv_ats_path"]:
        specimen_cv_path().unlink(missing_ok=True)
        return ""
    cv_file = config.user_path(settings["cv_ats_path"])
    if cv_file is None or not cv_file.is_file():
        specimen_cv_path().unlink(missing_ok=True)
        return f"Lebenslauf für Portale nicht gefunden: {cv_file}"
    try:
        pdf.html_to_pdf(cv_file.read_text(encoding="utf-8"), specimen_cv_path())
    except (OSError, UnicodeDecodeError, pdf.PdfError) as exc:
        specimen_cv_path().unlink(missing_ok=True)
        return f"Lebenslauf für Portale nicht gerendert: {exc}"
    return ""


async def build(job_id: int | None = None) -> dict:
    """Render the specimen Mappe. Seconds, headless Chrome, no LLM."""
    async with _lock:
        return await asyncio.to_thread(_build, job_id)
