"""Bewerbungsmappe assembly: HTML letter → PDF → one merged application PDF.

Chrome/Chromium headless renders the personal template faithfully (modern
CSS, embedded webfonts and photo) where LibreOffice does not — the flags are
ported from the proven legacy tracker. pypdf then appends the Anlagen
(certificates, references) so exactly ONE file goes out per application,
per German convention, ideally under 5 MB.
"""

import dataclasses
import io
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
# Re-encode only when the JPEG is a clear win. At parity the original wins:
# it is the bytes the user's own document already carries.
MAX_WORTHWHILE_RATIO = 0.9
# 4:4:4. Pillow defaults to 4:2:0, which halves the COLOUR resolution — on a
# certificate that is the red Stempel and the blue signature bleeding at their
# edges. Full chroma costs 153 KB on the real Mappe and leaves it inside every
# budget, so the legal documents keep their colour detail.
JPEG_SUBSAMPLING = 0


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
    """Page width/height in inches.

    /Rotate is deliberately NOT applied. It is a display instruction, while
    the mediabox and the matrix an image is drawn with both live in unrotated
    user space, so an image's pixel width maps to the mediabox width either
    way. Swapping the axes here read a true 300 dpi scan as 424 dpi, and at
    the floor rung that would have downsampled a 200 dpi scan to 141 —
    through the one limit this feature promises never to pass.
    """
    return float(page.mediabox.width) / 72.0, float(page.mediabox.height) / 72.0


def _effective_dpi(px_w: int, px_h: int, page_w_in: float, page_h_in: float) -> float:
    """Lower bound on the resolution an image is rendered at.

    The true figure needs the matrix the image is drawn with, which lives in
    the content stream; full-page coverage is exact for the scans that
    actually cost bytes. The two axes are combined with min() rather than
    max() deliberately: when an image's aspect does not match the page, the
    smaller of the two readings is the one that cannot cause over-
    downsampling, and staying above the quality floor matters more here than
    squeezing out the last kilobyte.

    It is a LOWER bound only for images drawn no larger than the page. One
    scaled up beyond the page edges reads higher than it truly is — see the
    residual noted in ROADMAP.
    """
    if page_w_in <= 0 or page_h_in <= 0:
        return 0.0
    return min(px_w / page_w_in, px_h / page_h_in)


def _colourspace_is_plain(colourspace) -> bool:
    """True for colourspaces JPEG reproduces faithfully: grey and RGB.

    /Indexed is excluded even though pypdf hands back a plain RGB image for
    it — the palette is what makes those files small and their edges sharp,
    and JPEG destroys both. CMYK, /Separation and /DeviceN are excluded
    because the round-trip changes the colour numbers a printer would use.
    """
    resolved = (colourspace.get_object()
                if hasattr(colourspace, "get_object") else colourspace)
    if isinstance(resolved, list):
        if not resolved or str(resolved[0]) != "/ICCBased":
            return False
        try:  # an ICC profile still wraps grey (N=1), RGB (3) or CMYK (4)
            return int(resolved[1].get_object().get("/N", 0)) in (1, 3)
        except Exception:  # noqa: BLE001 - malformed profile, treat as unknown
            return False
    return str(resolved) in ("/DeviceRGB", "/DeviceGray", "/CalRGB", "/CalGray")


def _skip_reason(image_obj, px_w: int, px_h: int, dpi: float, max_dpi: int) -> str:
    """Why this image must not be re-encoded, or "" if it may be.

    Every rule here answers "would re-encoding change what the reader sees,
    for a saving that does not justify it?" — and answers it from the object
    dictionary, which is the only description of the image that is not
    already an interpretation. The decoded image is a poor screen: pypdf
    hands back mode "L" for a 16-bit scan it has silently truncated, and
    mode "RGB" for an /Indexed one.
    """
    if "/SMask" in image_obj or "/Mask" in image_obj:
        return "transparency"  # JPEG carries no alpha channel
    if image_obj.get("/ImageMask"):
        return "stencil"
    if "/Decode" in image_obj:
        return "custom decode array"  # re-encoding would drop the inversion
    try:
        if int(image_obj.get("/BitsPerComponent", 8) or 8) != 8:
            return "not 8-bit per component"
    except (TypeError, ValueError):
        return "unreadable bit depth"
    if not _colourspace_is_plain(image_obj.get("/ColorSpace")):
        return "colourspace JPEG cannot reproduce"
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
            # One object drawn on several pages (the CV photo sits on the
            # Deckblatt and the Lebenslauf). Skipping it is a saving, not a
            # correctness guard: a second visit would find it already lossy
            # and within budget and refuse it anyway.
            if key in seen:
                continue
            seen.add(key)
            dpi = _effective_dpi(px_w, px_h, page_w_in, page_h_in)
            if _skip_reason(obj, px_w, px_h, dpi, max_dpi):
                continue
            try:
                pil = image.image
                if pil is None or pil.mode not in ("RGB", "L"):
                    continue  # a decode the dictionary screen did not predict
                before = len(image.data)
                if dpi > max_dpi * DPI_TOLERANCE:
                    scale = max_dpi / dpi
                    pil = pil.resize(
                        (max(1, round(pil.width * scale)),
                         max(1, round(pil.height * scale))),
                        Image.Resampling.LANCZOS,
                    )
                # Encode once to LOOK, before committing. JPEG is not a
                # universal win: a line-art or text scan is exactly what Flate
                # compresses well and JPEG compresses badly — one measured
                # here grew 17.9x AND gained ringing on every edge. The
                # file-level "never bigger" guard cannot catch that, because a
                # page that doubles is invisible next to three that shrink.
                probe = io.BytesIO()
                pil.save(probe, "JPEG", quality=quality, optimize=True,
                         subsampling=JPEG_SUBSAMPLING)
                if len(probe.getvalue()) > before * MAX_WORTHWHILE_RATIO:
                    continue
                image.replace(pil, quality=quality, optimize=True,
                              subsampling=JPEG_SUBSAMPLING)
            except Exception as exc:  # noqa: BLE001 - any decode/encode failure
                log.debug("leaving image %s untouched: %s", image.name, exc)
                continue
            recoded += 1
    # Lossless, and on a merged Mappe it is worth more than a whole ladder
    # rung: certificates from one issuer share a background image, and once
    # re-encoded those become byte-identical objects. Measured on the real
    # Mappe it removed 496 KB — 23% — at no quality cost, which is what lets
    # both budgets be met by the GENTLEST rung.
    # remove_unreferenced stays OFF: it added nothing on that corpus, and
    # dropping an object pypdf's reachability analysis missed is a torn PDF.
    try:
        writer.compress_identical_objects(remove_duplicates=True,
                                          remove_unreferenced=False)
    except Exception as exc:  # noqa: BLE001 - a saving, never a requirement
        log.debug("object de-duplication skipped: %s", exc)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_suffix(".pdf.part")
    try:
        with tmp_out.open("wb") as fh:
            writer.write(fh)
        tmp_out.replace(out)
    finally:
        tmp_out.unlink(missing_ok=True)
    return recoded


def install_pdf(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Copy a finished PDF to its finished location, atomically.

    The draft's stored pdf_path may already point at `dst`, so a torn
    half-copy there is a broken attachment, not merely a failed build."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst = dst.with_suffix(".pdf.part")
    try:
        shutil.copyfile(src, tmp_dst)
        tmp_dst.replace(dst)
    finally:
        tmp_dst.unlink(missing_ok=True)


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
        install_pdf(src, out)
        return Compression(size_bytes=original, original_bytes=original)

    best: Compression | None = None
    for dpi, quality in COMPRESSION_LADDER:
        attempt = out.with_suffix(".pdf.rung")
        try:
            recoded = compress_pdf(src, attempt, max_dpi=dpi, quality=quality)
        except Exception as exc:  # noqa: BLE001 - compression is best-effort
            log.warning("compressing %s at %s dpi failed: %s", src.name, dpi, exc)
            attempt.unlink(missing_ok=True)
            break
        size = attempt.stat().st_size
        result = Compression(size_bytes=size, original_bytes=original, dpi=dpi,
                             quality=quality, images_recoded=recoded,
                             met_target=size <= target_bytes)
        # Keep whichever rung came out smallest, not whichever ran last. The
        # ladder is monotone for every input I could construct, so this is
        # defensive: a harsher rung re-encodes MORE images (one the gentler
        # rung refused as not worth it may pass at a lower quality), and
        # nothing guarantees that trade always pays.
        if best is None or size < best.size_bytes:
            best = result
            attempt.replace(out)
        else:
            attempt.unlink(missing_ok=True)
        if best.met_target:
            break

    # Nothing gained (or nothing ran): the original is the better artefact.
    if best is None or best.size_bytes >= original:
        install_pdf(src, out)
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
