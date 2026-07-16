"""Bewerbungsmappe assembly: HTML letter → PDF → one merged application PDF.

Chrome/Chromium headless renders the personal template faithfully (modern
CSS, embedded webfonts and photo) where LibreOffice does not — the flags are
ported from the proven legacy tracker. pypdf then appends the Anlagen
(certificates, references) so exactly ONE file goes out per application,
per German convention, ideally under 5 MB.
"""

import pathlib
import re
import shutil
import subprocess
import tempfile

from pypdf import PdfReader, PdfWriter

MAX_MAPPE_BYTES = 5 * 1024 * 1024  # German-application convention: ONE PDF < 5 MB
CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable",
                     "chromium", "chromium-browser", "chrome")
RENDER_TIMEOUT_S = 180


class PdfError(RuntimeError):
    """PDF rendering or merging failed in a user-relevant way."""


def find_chrome() -> str | None:
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def safe_filename(name: str) -> str:
    """Company/applicant names → filesystem-safe ASCII-ish fragment."""
    replacements = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe",
                    "Ü": "Ue", "ß": "ss"}
    for src, dst in replacements.items():
        name = name.replace(src, dst)
    name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return name[:60]


def html_to_pdf(html_text: str, out_pdf: pathlib.Path) -> None:
    """Render an HTML string to PDF via headless Chrome.

    A unique temporary profile avoids the SingletonLock stall when the
    user's own Chrome is running — the classic 'PDF never appears' cause."""
    chrome = find_chrome()
    if chrome is None:
        raise PdfError(
            "Google Chrome / Chromium not found — required to render the "
            "letter template. Install google-chrome or chromium."
        )
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    out_pdf.unlink(missing_ok=True)  # so we know THIS run produced the file
    workdir = tempfile.mkdtemp(prefix="jobdeck_chrome_")
    try:
        html_path = pathlib.Path(workdir) / "mappe.html"
        html_path.write_text(html_text, encoding="utf-8")
        cmd = [
            chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
            f"--user-data-dir={workdir}/profile",
            "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--disable-background-networking",
            "--no-pdf-header-footer",
            "--virtual-time-budget=5000",  # fonts are embedded — no network waits
            f"--print-to-pdf={out_pdf}",
            html_path.as_uri(),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=RENDER_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise PdfError(
                f"Chrome did not finish rendering within {RENDER_TIMEOUT_S}s "
                f"— close other Chrome instances and retry"
            ) from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if not out_pdf.exists():
        raise PdfError(
            "HTML → PDF conversion failed (Chrome: "
            f"{chrome}): {proc.stderr or proc.stdout or 'no output'}"
        )


def merge_pdfs(parts: list[pathlib.Path], out_pdf: pathlib.Path) -> None:
    """Concatenate PDFs in order into out_pdf. Unreadable parts fail loudly.

    Writes to a sibling temp file and renames atomically, so a mid-write
    failure never leaves a torn PDF at a path something already links to."""
    writer = PdfWriter()
    for part in parts:
        try:
            writer.append(str(part))
        except Exception as exc:
            raise PdfError(f"cannot merge {part.name}: {exc}") from exc
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    tmp_pdf = out_pdf.with_suffix(".pdf.part")
    try:
        with tmp_pdf.open("wb") as fh:
            writer.write(fh)
        tmp_pdf.replace(out_pdf)
    finally:
        tmp_pdf.unlink(missing_ok=True)


def page_count(pdf_path: pathlib.Path) -> int:
    return len(PdfReader(str(pdf_path)).pages)


def collect_anlagen(anlagen_dir: str) -> list[pathlib.Path]:
    """PDFs from the Anlagen folder, sorted by filename (prefix 01_, 02_ …
    to control the order). Empty/missing dir → no Anlagen."""
    if not (anlagen_dir or "").strip():
        return []
    folder = pathlib.Path(anlagen_dir).expanduser()
    if not folder.is_dir():
        raise PdfError(f"Anlagen folder does not exist: {folder}")
    return sorted(p for p in folder.iterdir() if p.suffix.lower() == ".pdf")
