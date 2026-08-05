"""Bewerbungsmappe assembly: HTML letter → PDF → one merged application PDF.

Chrome/Chromium headless renders the personal template faithfully (modern
CSS, embedded webfonts and photo) where LibreOffice does not — the flags are
ported from the proven legacy tracker. pypdf then appends the Anlagen
(certificates, references) so exactly ONE file goes out per application,
per German convention, ideally under 5 MB.
"""

import dataclasses
import logging
import pathlib
import re
import shutil
import subprocess
import tempfile

from PIL import Image
from pypdf import PdfReader, PdfWriter

log = logging.getLogger(__name__)

MAX_MAPPE_BYTES = 5 * 1024 * 1024  # German-application convention: ONE PDF < 5 MB
CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable",
                     "chromium", "chromium-browser", "chrome")
RENDER_TIMEOUT_S = 180

# Compression ladder, best quality first; the LAST rung is the quality floor
# and is never passed. 200 dpi / q80 is the level Andrei accepted after seeing
# it side by side with the 600-dpi original at equal zoom (2026-07-17) — below
# it a scanned Zeugnis starts to soften visibly, and an unreadable certificate
# costs far more than a large attachment.
COMPRESSION_LADDER = ((300, 85), (250, 82), (200, 80))
LOSSY_FILTERS = frozenset({"/DCTDecode", "/JPXDecode"})
# Under ~450x450 an image is a logo, signature or stamp: re-encoding saves a
# few KB while the generation loss is proportionally most visible there.
MIN_COMPRESS_PIXELS = 200_000
# Only downsample when an image is meaningfully over the target, so a scan
# already produced at the target dpi is left byte-identical.
DPI_TOLERANCE = 1.05


class PdfError(RuntimeError):
    """PDF rendering or merging failed in a user-relevant way."""


@dataclasses.dataclass(frozen=True)
class Compression:
    """Outcome of fitting a PDF to a size budget.

    `dpi`/`quality` are None when the file was already small enough and was
    therefore passed through untouched — the no-loss case.
    """

    size_bytes: int
    original_bytes: int
    dpi: int | None = None
    quality: int | None = None
    images_recoded: int = 0
    met_target: bool = True

    @property
    def applied(self) -> bool:
        return self.dpi is not None

    def describe(self) -> str:
        if not self.applied:
            return ""
        return (f"{self.original_bytes / 1024 / 1024:.2f} MB → "
                f"{self.size_bytes / 1024 / 1024:.2f} MB "
                f"({self.dpi} dpi, q{self.quality}, "
                f"{self.images_recoded} image(s))")


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


def _page_size_inches(page) -> tuple[float, float]:
    """Page width/height in inches, accounting for /Rotate."""
    width = float(page.mediabox.width) / 72.0
    height = float(page.mediabox.height) / 72.0
    try:
        rotation = int(page.get("/Rotate", 0) or 0) % 360
    except (TypeError, ValueError):
        rotation = 0
    if rotation in (90, 270):
        width, height = height, width
    return width, height


def _effective_dpi(px_w: int, px_h: int, page_w_in: float, page_h_in: float) -> float:
    """Resolution an image is rendered at, assuming it covers the page.

    The true value needs the CTM the image is drawn with, which lives in the
    content stream. Assuming full-page coverage is exact for the scans that
    actually cost bytes and UNDER-estimates every smaller placement, so the
    error is always in the safe direction: an image is never downsampled
    harder than this figure implies.
    """
    if page_w_in <= 0 or page_h_in <= 0:
        return 0.0
    return max(px_w / page_w_in, px_h / page_h_in)


def _skip_reason(image_obj, px_w: int, px_h: int, dpi: float, max_dpi: int) -> str:
    """Why this image must not be re-encoded, or "" if it may be.

    Every rule here answers "would re-encoding change what the reader sees,
    for a saving that does not justify it?" — decided from the object
    dictionary alone, so a skipped image is never even decoded.
    """
    if "/SMask" in image_obj or "/Mask" in image_obj:
        return "transparency"  # JPEG carries no alpha channel
    if image_obj.get("/ImageMask"):
        return "stencil"
    if "/Decode" in image_obj:
        return "custom decode array"  # re-encoding would drop the inversion
    if px_w * px_h < MIN_COMPRESS_PIXELS:
        return "small"
    filters = image_obj.get("/Filter")
    names = {str(f) for f in (filters if isinstance(filters, list) else [filters])}
    if names & LOSSY_FILTERS and dpi <= max_dpi * DPI_TOLERANCE:
        # Already lossy and already within budget: a second JPEG pass costs
        # real generation loss. Measured on the real Mappe (2026-08-05): it
        # bought 3-8% per image and the merged total did not change at all.
        return "already compressed"
    return ""


def compress_pdf(src: pathlib.Path, out: pathlib.Path, *,
                 max_dpi: int, quality: int) -> int:
    """Write `src` to `out` with its large images re-encoded as JPEG.

    Images above `max_dpi` are downsampled to it; losslessly stored images
    are re-encoded even when within budget, because that is where the bytes
    hide. Returns how many images were actually rewritten. An image that
    cannot be decoded or replaced is left exactly as it was — a Mappe that
    is too large still gets sent, one with a corrupted Zeugnis does not.
    """
    writer = PdfWriter(clone_from=str(src))
    seen: set[tuple[int, int]] = set()
    recoded = 0
    for page in writer.pages:
        page_w_in, page_h_in = _page_size_inches(page)
        # Indexed rather than iterated: building an ImageFile decodes the
        # image, so one corrupt object would otherwise abort the whole page
        # and cost the other nine their compression.
        for index in range(len(page.images)):
            try:
                image = page.images[index]
                ref = image.indirect_reference
                obj = ref.get_object()
                px_w, px_h = int(obj["/Width"]), int(obj["/Height"])
            except Exception as exc:  # noqa: BLE001 - unreadable image object
                log.debug("skipping unreadable image %s: %s", index, exc)
                continue
            key = (ref.idnum, ref.generation)
            if key in seen:  # one object, drawn on several pages
                continue
            seen.add(key)
            dpi = _effective_dpi(px_w, px_h, page_w_in, page_h_in)
            if _skip_reason(obj, px_w, px_h, dpi, max_dpi):
                continue
            try:
                pil = image.image
                if pil is None or pil.mode not in ("RGB", "L"):
                    continue  # CMYK, palette and 1-bit survive re-encoding badly
                if dpi > max_dpi * DPI_TOLERANCE:
                    scale = max_dpi / dpi
                    pil = pil.resize(
                        (max(1, round(px_w * scale)), max(1, round(px_h * scale))),
                        Image.Resampling.LANCZOS,
                    )
                image.replace(pil, quality=quality, optimize=True)
            except Exception as exc:  # noqa: BLE001 - any decode/encode failure
                log.debug("leaving image %s untouched: %s", image.name, exc)
                continue
            recoded += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_suffix(".pdf.part")
    try:
        with tmp_out.open("wb") as fh:
            writer.write(fh)
        tmp_out.replace(out)
    finally:
        tmp_out.unlink(missing_ok=True)
    return recoded


def compress_to_target(src: pathlib.Path, out: pathlib.Path,
                       target_bytes: int) -> Compression:
    """Fit `src` into `target_bytes`, never going below the quality floor.

    Walks COMPRESSION_LADDER from best quality down and stops at the first
    rung that meets the budget, so the document is degraded only as far as
    the target actually demands. A file already within budget is copied
    through untouched. If even the floor rung misses the target, that rung's
    output is kept and `met_target` is False — the caller warns, because
    shrinking further would cost legibility, and a slightly oversized
    application is better than an unreadable one.
    """
    original = src.stat().st_size
    if original <= target_bytes:
        shutil.copyfile(src, out)
        return Compression(size_bytes=original, original_bytes=original)

    best: Compression | None = None
    for dpi, quality in COMPRESSION_LADDER:
        try:
            recoded = compress_pdf(src, out, max_dpi=dpi, quality=quality)
        except Exception as exc:  # noqa: BLE001 - compression is best-effort
            log.warning("compressing %s at %s dpi failed: %s", src.name, dpi, exc)
            break
        size = out.stat().st_size
        best = Compression(size_bytes=size, original_bytes=original, dpi=dpi,
                           quality=quality, images_recoded=recoded,
                           met_target=size <= target_bytes)
        if best.met_target:
            return best

    # Nothing gained (or nothing ran): the original is the better artefact.
    if best is None or best.size_bytes >= original:
        shutil.copyfile(src, out)
        return Compression(size_bytes=original, original_bytes=original,
                           met_target=original <= target_bytes)
    return best


def collect_anlagen(anlagen_dir: str) -> list[pathlib.Path]:
    """PDFs from the Anlagen folder, sorted by filename (prefix 01_, 02_ …
    to control the order). Empty/missing dir → no Anlagen."""
    if not (anlagen_dir or "").strip():
        return []
    folder = pathlib.Path(anlagen_dir).expanduser()
    if not folder.is_dir():
        raise PdfError(f"Anlagen folder does not exist: {folder}")
    return sorted(p for p in folder.iterdir() if p.suffix.lower() == ".pdf")
