"""One-click Bewerbungsmappe for a ready draft.

Takes the drafted Anschreiben, renders it into the user's personal letter
template (headless Chrome), appends the Anlagen PDFs and writes exactly ONE
`Bewerbung_<Name>_<Firma>.pdf` into a per-job output folder — the file the
send slice will attach. No LLM involved, no e-mail is sent here.

The per-job folder keeps the recipient-facing filename clean (German
convention) while two postings at the same company can never overwrite each
other's Mappe. A build is linked to the exact draft revision it was read
from: if the draft changes while Chrome renders, the result is discarded
instead of being blessed as the new draft's PDF.
"""

import asyncio
import logging
import pathlib
import tempfile

from jobdeck import apply_channel, config, db, pdf, templates
from jobdeck import settings as app_settings
from jobdeck.ai import drafting as ai_drafting
from jobdeck.dates import heute_de
from jobdeck.services import upload
from jobdeck.services.drafting import resolve_refnr

log = logging.getLogger(__name__)

_lock = asyncio.Lock()  # double-clicks must not race Chrome on one output file

# A Mappe may be (re)built for a draft the user is still working on — an
# approved draft included: editing its letter clears the PDF, and it must
# be possible to get one back without un-approving first.
EDITABLE_STATUS = ("ready", "approved")

# What `jobs.mappe_kind` says when the current legacy build finishes. The
# implementation emits one complete package rather than versioned, selectable
# artifacts. The accepted target is documented in
# `docs/adr/0005-job-specific-application-documents.md`.
MAPPE_COMPLETE = "vollständig"

# Size budgets, in MB. E-mail: 2-3 MB is the deliverability sweet spot and
# corporate gateways start rejecting well below the 5 MB convention.
DEFAULT_TARGET_MB = 3.0
# Upload forms cap lower — frequently 2 MB, sometimes per file.
DEFAULT_PORTAL_TARGET_MB = 2.0
PORTAL_CHANNELS = frozenset({
    apply_channel.CHANNEL_ATS,
    apply_channel.CHANNEL_BOARD,
    apply_channel.CHANNEL_COMPANY_SITE,
})


def letter_values(job, draft, applicant_name: str, applicant_ort: str) -> dict:
    """The tokens the letter template is filled with, for THIS posting.

    `draft` may be None: a specimen Mappe is built to show what an employer
    receives before any application exists, and every field except the letter
    body is already decided by the posting and the user's settings.

    This is deliberately the ONLY place those values are derived. The preview
    screen exists to say "this field will be empty" before the PDF is built,
    and a preview computing the address block its own way would be reassuring
    about a letter it had not actually described.
    """
    strasse, plz_ort = templates.letter_address(job)
    betreff = draft["betreff"] if draft is not None else ""
    return {
        "firma": job["company"],
        "ansprechpartner": job["ansprechpartner"],
        "strasse": strasse,
        "plz_ort": plz_ort,
        "ort": applicant_ort,
        "datum": heute_de(),
        # Follows the (possibly user-corrected) e-mail subject, so the letter
        # and the e-mail never cite a different Stellenbezeichnung or Refnr.
        "betreff": (ai_drafting.letter_betreff(betreff, applicant_name)
                    or ai_drafting.build_betreff(job["title"],
                                                 resolve_refnr(job))),
        # Derived from the same subject: a cover sheet naming a different
        # Stelle than the letter is the classic copy-paste tell. The fallback
        # cleans the title too — without a draft (the specimen) it is ALWAYS
        # the fallback that runs, and the Betreff beside it is built from
        # `build_betreff`, which cleans. Page one would carry the board's
        # "Ab sofort:" while page two carried the tidy line.
        "deckblatt_rolle": ai_drafting.deckblatt_rolle(betreff, applicant_name)
                           or f"als {ai_drafting.clean_title(job['title'])}",
    }


def _error(message: str) -> dict:
    return {"ok": False, "error": message, "pdf_path": "", "warning": "",
            "pages": 0, "size_bytes": 0, "size_before_bytes": 0,
            "compression": "", "anlagen": []}


def target_mb_setting(raw: str, fallback: float) -> float:
    """A size budget in MB from a settings string, or the default.

    float() accepts "inf" and "1e400", and the infinity that comes back then
    raises OverflowError on the conversion to bytes — past the build's
    error handler, so the button just dies. app_settings holds strings in a
    directory the user is invited to edit, so the value has to be screened
    for being a real number, not merely for parsing.
    """
    return app_settings.parse_float(raw, fallback, minimum_exclusive=0)


# field -> (app_settings key, default, strip?). The single definition of what
# a Mappe is built from: `build_settings` reads it and BUILD_SETTING_KEYS is
# DERIVED from it. Two hand-kept lists drift, and the drift is invisible —
# the next setting added would become a fact a screen states and no signature
# can see, with every test still green.
_BUILD_SETTINGS = (
    ("applicant_name", "applicant_name", "", True),
    ("applicant_ort", "applicant_ort", "", True),
    ("template_path", "template_path", "", True),
    ("anlagen_dir", "anlagen_dir", "", True),
    # The one-column Lebenslauf a portal's parser reads in order. Optional:
    # without it the CV part is the Mappe's own CV page.
    ("cv_ats_path", "cv_ats_path", "", True),
    ("compress", "mappe_compress", "1", False),
    ("target_mb", "mappe_target_mb", "", False),
    ("target_portal_mb", "mappe_target_portal_mb", "", False),
)

# A screen stating the budgets, the template or the Anlagen folder it will use
# must rebuild when one of them changes on the Settings page — no table
# signature can see an app_settings row.
BUILD_SETTING_KEYS = tuple(key for _field, key, _default, _strip
                           in _BUILD_SETTINGS)


def build_settings(con) -> dict:
    """Everything outside the posting that decides what the Mappe becomes.

    One definition, because the specimen the user inspects and the Mappe an
    employer receives have to be built from the same template, the same
    Anlagen folder and the same size budgets — a screen describing documents
    assembled under different settings is worse than no screen.
    """
    values = {
        field: (db.get_setting(con, key, default).strip() if strip
                else db.get_setting(con, key, default))
        for field, key, default, strip in _BUILD_SETTINGS
    }
    values["compress"] = app_settings.parse_bool(values["compress"], True)
    return values


# The file names a portal's upload dialog shows for the parts. The company is
# in every one of them for the same reason it is in the Mappe's: the upload
# folder is flat and holds several applications at once.
PART_STEMS = {
    db.DOC_ANSCHREIBEN: "Anschreiben",
    db.DOC_LEBENSLAUF: "Lebenslauf",
    db.DOC_ANLAGEN: "Anlagen",
}


def wants_parts(job) -> bool:
    """Whether this posting's application goes through a form, where a
    portal asks for the letter, the CV and the certificates as separate files.

    A form he already opened counts regardless of what the channel column
    says: the column is a derivation that can lag, the opened form is a
    fact."""
    return bool(job["form_opened_at"]) or (
        (job["apply_channel"] or "") in PORTAL_CHANNELS)


def letter_page_range(rendered_pages: int) -> tuple[int, int]:
    """Which pages of the rendered template ARE the Anschreiben.

    The template's contract (`unterlagen.TEMPLATE_LABEL`) is Deckblatt ·
    Anschreiben · Lebenslauf, so with three or more pages the letter is
    everything between the first and the last. A template that renders fewer
    pages has no cover sheet or CV to strip, and the whole render is the
    letter — a one-page letter template is the simplest possible shape and
    must not lose its only page to this rule."""
    if rendered_pages >= 3:
        return 2, rendered_pages - 1
    return 1, max(rendered_pages, 1)


def target_bytes(settings: dict, channel: str) -> int:
    """Size budget for the channel this Mappe will travel through.

    An unresolved channel gets the e-mail budget rather than the tighter
    portal one: an oversized upload fails loudly in front of the user, who
    can rebuild, whereas needlessly degrading a scan is silent and permanent
    for that send.
    """
    if channel in PORTAL_CHANNELS:
        mb = target_mb_setting(settings["target_portal_mb"], DEFAULT_PORTAL_TARGET_MB)
    else:
        mb = target_mb_setting(settings["target_mb"], DEFAULT_TARGET_MB)
    return int(mb * 1024 * 1024)


def _build_mappe(job_id: int) -> dict:
    """Synchronous pipeline — runs in a worker thread."""
    with db.db() as con:
        draft = db.get_draft_by_job(con, job_id)
        job = db.get_job(con, job_id)
        settings = build_settings(con)
    if job is None:
        return _error("posting not found")
    if draft is None or draft["status"] not in EDITABLE_STATUS:
        return _error("draft the application first — the Mappe needs the "
                      "finished Anschreiben")
    if not draft["anschreiben_body"].strip():
        return _error("the draft has no Anschreiben — the letter page would "
                      "be an empty skeleton; re-draft it")
    if not settings["applicant_name"]:
        return _error("set your applicant name in Settings first")
    if not settings["applicant_ort"]:
        return _error("set your city (Ort) in Settings first — it heads "
                      "the letter's date line")
    if not settings["template_path"]:
        return _error("set the letter template path in Settings first")
    template_file = config.user_path(settings["template_path"])
    if template_file is None or not template_file.is_file():
        return _error(f"letter template not found: {template_file}")
    draft_revision = draft["updated_at"]

    values = letter_values(job, draft, settings["applicant_name"],
                           settings["applicant_ort"])
    values["anschreiben_body"] = draft["anschreiben_body"]
    try:
        template_html = template_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _error(f"cannot read the letter template: {exc}")

    try:
        letter_html = templates.render_letter(template_html, values)
        anlagen = pdf.collect_anlagen(settings["anlagen_dir"])

        name_part = pdf.safe_filename(settings["applicant_name"])
        firma_part = pdf.safe_filename(job["company"]) or "Initiativ"
        out_name = "_".join(p for p in ("Bewerbung", name_part, firma_part) if p)
        out_path = (pathlib.Path(config.OUTPUT_DIR) / f"job_{job_id}"
                    / f"{out_name}.pdf")

        budget = target_bytes(settings, job["apply_channel"] or "")
        parts: list[dict] = []
        with tempfile.TemporaryDirectory(prefix="jobdeck_mappe_") as tmp:
            letter_pdf = pathlib.Path(tmp) / "anschreiben.pdf"
            pdf.html_to_pdf(letter_html, letter_pdf)
            # Merged in the temp dir, then fitted to the budget on the way
            # out: the Anlagen the user curated are only ever READ, and the
            # compression works on the same bytes that will be attached.
            merged = pathlib.Path(tmp) / "mappe.pdf"
            pdf.merge_pdfs([letter_pdf, *anlagen], merged)
            if settings["compress"]:
                compression = pdf.compress_to_target(merged, out_path, budget)
            else:
                pdf.install_pdf(merged, out_path)
                merged_size = merged.stat().st_size
                compression = pdf.Compression(
                    size_bytes=merged_size, original_bytes=merged_size,
                    met_target=merged_size <= budget,
                )
            if wants_parts(job):
                parts = _build_parts(job_id, settings, letter_pdf, anlagen,
                                     pathlib.Path(tmp), out_path.parent,
                                     name_part, firma_part)
        size = out_path.stat().st_size
        pages = pdf.page_count(out_path)
    except (templates.TemplateError, pdf.PdfError, OSError) as exc:
        return _error(str(exc))

    if compression.applied:
        log.info("mappe for job %s compressed: %s", job_id,
                 compression.describe())
    warning = ""
    if size > pdf.MAX_MAPPE_BYTES:
        warning = (f"Mappe is {size / 1024 / 1024:.1f} MB — over the 5 MB "
                   f"convention; remove or pre-shrink an Anlage")
        log.warning("mappe for job %s: %s", job_id, warning)
    elif not compression.met_target:
        # Name the reason that actually applies: telling someone the quality
        # floor is in the way, when they simply switched shrinking off, sends
        # them looking for a limit instead of a switch.
        reason = ("the quality floor stops further compression"
                  if settings["compress"]
                  else "shrinking is switched off in Settings")
        warning = (f"Mappe is {size / 1024 / 1024:.1f} MB — over the "
                   f"{budget / 1024 / 1024:.1f} MB target for this "
                   f"channel; {reason}")
        log.warning("mappe for job %s: %s", job_id, warning)

    with db.db() as con:
        current = db.get_draft_by_job(con, job_id)
        # updated_at has second resolution — also compare the text the PDF
        # actually rendered, which is the invariant it must match.
        if (current is None or current["status"] not in EDITABLE_STATUS
                or current["updated_at"] != draft_revision
                or current["anschreiben_body"] != draft["anschreiben_body"]
                or current["betreff"] != draft["betreff"]):
            # The draft was regenerated while Chrome rendered — this PDF
            # holds the OLD text and must not be linked to the new draft.
            out_path.unlink(missing_ok=True)
            for part in parts:
                pathlib.Path(part["path"]).unlink(missing_ok=True)
            upload.clear(job["upload_path"])
            for row in db.list_documents(con, job_id):
                upload.clear(row["staged_path"])
            db.clear_documents(con, job_id)
            db.set_upload(con, job_id, "", "")
            return _error("the draft changed while the Mappe was rendering "
                          "— create the PDF again for the new text")
        db.upsert_draft(con, job_id, {"pdf_path": str(out_path)})
        documents = [{"kind": db.DOC_MAPPE, "path": str(out_path),
                      "pages": pages, "bytes": size,
                      "sha256": pdf.sha256_of(out_path)}, *parts]
        # Where the previous build's files were offered, so a rebuild lands on
        # its own links instead of walking one "(2)" further every time.
        previously = {row["kind"]: row["staged_path"]
                      for row in db.list_documents(con, job_id)}
        # Staged only for an application that is actually being uploaded to a
        # form. Every channel builds a Mappe — the review-and-send editor and
        # the prepare batch both come through here — and only the form
        # recorder ever takes a file back out, so staging unconditionally
        # would fill the folder with documents nobody is uploading and nothing
        # would ever remove them.
        #
        # Staged HERE and nowhere earlier, for two reasons that both end with
        # the employer opening the wrong file. `compress_to_target` writes
        # `out` up to four times in one call and both installers end in
        # `replace()`, so a link made mid-build points at bytes that are no
        # longer the Mappe. And a link made before the revision check above
        # would keep the unlinked inode ALIVE: the upload folder would hold a
        # complete, plausible Mappe carrying the OLD letter while the app
        # reported the build failed — and that is the file the picker offers.
        if job["form_opened_at"]:
            staged = upload.stage(out_path, job["upload_path"])
            db.set_upload(con, job_id, str(staged), MAPPE_COMPLETE)
            for doc in documents:
                if doc["kind"] == db.DOC_MAPPE:
                    # already staged above, under `upload_path` — staging it
                    # a second time would land beside itself as "… (2).pdf"
                    doc["staged_path"] = str(staged)
                    continue
                doc["staged_path"] = str(upload.stage(
                    pathlib.Path(doc["path"]), previously.get(doc["kind"], "")))
            # A part the previous build staged and this one did not produce
            # must leave the folder, or the picker offers a file the strip
            # no longer lists.
            for kind, old_path in previously.items():
                if old_path and kind not in {d["kind"] for d in documents}:
                    upload.clear(old_path)
        db.set_documents(con, job_id, documents)
    return {"ok": True, "error": "", "pdf_path": str(out_path),
            "warning": warning, "pages": pages, "size_bytes": size,
            "size_before_bytes": compression.original_bytes,
            "compression": compression.describe(),
            "anlagen": [p.name for p in anlagen],
            "documents": documents}


def _build_parts(job_id: int, settings: dict, letter_pdf: pathlib.Path,
                 anlagen: list[pathlib.Path], tmp: pathlib.Path,
                 job_dir: pathlib.Path, name_part: str,
                 firma_part: str) -> list[dict]:
    """The three files a portal's form asks for, beside the full Mappe.

    Each is fitted to the PORTAL budget on its own — upload forms cap per file
    — and lands in the same per-job archive folder as the Mappe. The CV is the
    one-column ATS Lebenslauf when one is configured; otherwise the Mappe's
    own CV page, so a portal never gets no CV at all.
    """
    rendered = pdf.page_count(letter_pdf)
    first, last = letter_page_range(rendered)
    sources: list[tuple[str, pathlib.Path]] = []
    brief = tmp / "brief.pdf"
    pdf.extract_pages(letter_pdf, brief, first, last)
    sources.append((db.DOC_ANSCHREIBEN, brief))

    cv_file = config.user_path(settings["cv_ats_path"])
    cv = tmp / "lebenslauf.pdf"
    if cv_file is not None and cv_file.is_file():
        pdf.html_to_pdf(cv_file.read_text(encoding="utf-8"), cv)
        sources.append((db.DOC_LEBENSLAUF, cv))
    elif rendered >= 3:
        pdf.extract_pages(letter_pdf, cv, rendered, rendered)
        sources.append((db.DOC_LEBENSLAUF, cv))
    if anlagen:
        merged = tmp / "anlagen.pdf"
        pdf.merge_pdfs(anlagen, merged)
        sources.append((db.DOC_ANLAGEN, merged))

    budget = target_bytes(settings, apply_channel.CHANNEL_ATS)
    parts = []
    for kind, src in sources:
        out = job_dir / "_".join(
            p for p in (PART_STEMS[kind], name_part, firma_part) if p)
        out = out.with_suffix(".pdf")
        if settings["compress"]:
            compression = pdf.compress_to_target(src, out, budget)
            if not compression.met_target:
                log.warning("part %s for job %s is %.1f MB — over the portal "
                            "budget", kind, job_id, out.stat().st_size / 1024 / 1024)
        else:
            pdf.install_pdf(src, out)
        parts.append({"kind": kind, "path": str(out),
                      "pages": pdf.page_count(out),
                      "bytes": out.stat().st_size,
                      "sha256": pdf.sha256_of(out)})
    return parts


async def create_mappe(job_id: int) -> dict:
    """Build the application PDF for a job's ready draft.

    Returns {"ok", "error", "pdf_path", "warning", "pages", "size_bytes",
    "size_before_bytes", "compression", "anlagen"}."""
    async with _lock:  # serialize concurrent Create PDF clicks
        return await asyncio.to_thread(_build_mappe, job_id)
