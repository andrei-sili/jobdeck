"""One-click Bewerbungsmappe for a ready draft.

Takes the drafted Anschreiben, renders it into the user's personal letter
template (headless Chrome), appends the Anlagen PDFs and writes exactly ONE
`Bewerbung_<Name>_<Firma>.pdf` into the output directory — the file the
send slice will attach. No LLM involved, no e-mail is sent here.
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


def _error(message: str) -> dict:
    return {"ok": False, "error": message, "pdf_path": "", "warning": "",
            "pages": 0, "size_bytes": 0}


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
    if draft is None or draft["status"] != "ready":
        return _error("draft the application first — the Mappe needs the "
                      "finished Anschreiben")
    if not settings["applicant_name"]:
        return _error("set your applicant name in Settings first")
    if not settings["template_path"]:
        return _error("set the letter template path in Settings first")
    template_file = pathlib.Path(settings["template_path"]).expanduser()
    if not template_file.is_file():
        return _error(f"letter template not found: {template_file}")

    values = {
        "firma": job["company"],
        "ansprechpartner": job["ansprechpartner"],
        "strasse": job["contact_strasse"],
        "plz_ort": job["contact_plz_ort"],
        "ort": settings["applicant_ort"],
        "datum": heute_de(),
        # letter subject omits the applicant name — it already heads the letter
        "betreff": ai_drafting.build_betreff(job["title"], resolve_refnr(job)),
        "anschreiben_body": draft["anschreiben_body"],
    }
    try:
        letter_html = templates.render_letter(
            template_file.read_text(encoding="utf-8"), values
        )
        anlagen = pdf.collect_anlagen(settings["anlagen_dir"])

        firma_part = pdf.safe_filename(job["company"]) or "Initiativ"
        out_name = f"Bewerbung_{pdf.safe_filename(settings['applicant_name'])}_{firma_part}.pdf"
        out_path = pathlib.Path(config.OUTPUT_DIR) / out_name

        with tempfile.TemporaryDirectory(prefix="jobdeck_mappe_") as tmp:
            letter_pdf = pathlib.Path(tmp) / "anschreiben.pdf"
            pdf.html_to_pdf(letter_html, letter_pdf)
            pdf.merge_pdfs([letter_pdf, *anlagen], out_path)
    except (templates.TemplateError, pdf.PdfError) as exc:
        return _error(str(exc))

    size = out_path.stat().st_size
    warning = ""
    if size > pdf.MAX_MAPPE_BYTES:
        warning = (f"Mappe is {size / 1024 / 1024:.1f} MB — over the 5 MB "
                   f"convention; consider compressing the Anlagen")
        log.warning("mappe for job %s: %s", job_id, warning)

    with db.db() as con:
        db.upsert_draft(con, job_id, {"pdf_path": str(out_path)})
    return {"ok": True, "error": "", "pdf_path": str(out_path),
            "warning": warning, "pages": pdf.page_count(out_path),
            "size_bytes": size}


async def create_mappe(job_id: int) -> dict:
    """Build the application PDF for a job's ready draft.

    Returns {"ok", "error", "pdf_path", "warning", "pages", "size_bytes"}."""
    return await asyncio.to_thread(_build_mappe, job_id)
