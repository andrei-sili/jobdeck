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

from jobdeck import config, db, pdf, templates
from jobdeck.ai import drafting as ai_drafting
from jobdeck.dates import heute_de
from jobdeck.services.drafting import resolve_refnr

log = logging.getLogger(__name__)

_lock = asyncio.Lock()  # double-clicks must not race Chrome on one output file

# A Mappe may be (re)built for a draft the user is still working on — an
# approved draft included: editing its letter clears the PDF, and it must
# be possible to get one back without un-approving first.
EDITABLE_STATUS = ("ready", "approved")


def _error(message: str) -> dict:
    return {"ok": False, "error": message, "pdf_path": "", "warning": "",
            "pages": 0, "size_bytes": 0, "anlagen": []}


def _build_mappe(job_id: int) -> dict:
    """Synchronous pipeline — runs in a worker thread."""
    with db.db() as con:
        draft = db.get_draft_by_job(con, job_id)
        job = db.get_job(con, job_id)
        settings = {
            "applicant_name": db.get_setting(con, "applicant_name", "").strip(),
            "applicant_ort": db.get_setting(con, "applicant_ort", "").strip(),
            "template_path": db.get_setting(con, "template_path", "").strip(),
            "anlagen_dir": db.get_setting(con, "anlagen_dir", "").strip(),
        }
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
    template_file = pathlib.Path(settings["template_path"]).expanduser()
    if not template_file.is_file():
        return _error(f"letter template not found: {template_file}")
    draft_revision = draft["updated_at"]

    values = {
        "firma": job["company"],
        "ansprechpartner": job["ansprechpartner"],
        "strasse": job["contact_strasse"],
        "plz_ort": job["contact_plz_ort"],
        "ort": settings["applicant_ort"],
        "datum": heute_de(),
        # Follows the (possibly user-corrected) e-mail subject, so the letter
        # and the e-mail never cite a different Stellenbezeichnung or Refnr.
        "betreff": (ai_drafting.letter_betreff(draft["betreff"],
                                               settings["applicant_name"])
                    or ai_drafting.build_betreff(job["title"],
                                                 resolve_refnr(job))),
        "anschreiben_body": draft["anschreiben_body"],
    }
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

        with tempfile.TemporaryDirectory(prefix="jobdeck_mappe_") as tmp:
            letter_pdf = pathlib.Path(tmp) / "anschreiben.pdf"
            pdf.html_to_pdf(letter_html, letter_pdf)
            pdf.merge_pdfs([letter_pdf, *anlagen], out_path)
        size = out_path.stat().st_size
        pages = pdf.page_count(out_path)
    except (templates.TemplateError, pdf.PdfError) as exc:
        return _error(str(exc))

    warning = ""
    if size > pdf.MAX_MAPPE_BYTES:
        warning = (f"Mappe is {size / 1024 / 1024:.1f} MB — over the 5 MB "
                   f"convention; consider compressing the Anlagen")
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
            return _error("the draft changed while the Mappe was rendering "
                          "— create the PDF again for the new text")
        db.upsert_draft(con, job_id, {"pdf_path": str(out_path)})
    return {"ok": True, "error": "", "pdf_path": str(out_path),
            "warning": warning, "pages": pages, "size_bytes": size,
            "anlagen": [p.name for p in anlagen]}


async def create_mappe(job_id: int) -> dict:
    """Build the application PDF for a job's ready draft.

    Returns {"ok", "error", "pdf_path", "warning", "pages", "size_bytes",
    "anlagen"}."""
    async with _lock:  # serialize concurrent Create PDF clicks
        return await asyncio.to_thread(_build_mappe, job_id)
