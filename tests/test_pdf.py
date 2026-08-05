import io
import pathlib
import random
import zlib

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    NameObject,
    NumberObject,
)

from jobdeck import pdf


def _blank_pdf(path: pathlib.Path, pages: int = 1) -> pathlib.Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)  # A4 points
    with path.open("wb") as fh:
        writer.write(fh)
    return path


def _noisy(width: int, height: int) -> Image.Image:
    """Incompressible content — a smooth image would shrink under Flate and
    make a 'compression saved bytes' assertion pass for the wrong reason."""
    rng = random.Random(7)
    image = Image.new("RGB", (width, height))
    image.putdata([(rng.randrange(256),) * 3 for _ in range(width * height)])
    return image


def _image_xobject(writer: PdfWriter, image: Image.Image, *, lossless: bool,
                   smask: bool = False) -> EncodedStreamObject:
    if lossless:
        data = zlib.compress(image.tobytes(), 9)
        filt = NameObject("/FlateDecode")
    else:
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=95)
        data, filt = buf.getvalue(), NameObject("/DCTDecode")
    stream = EncodedStreamObject()
    stream.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(image.width),
        NameObject("/Height"): NumberObject(image.height),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
        NameObject("/BitsPerComponent"): NumberObject(8),
        NameObject("/Filter"): filt,
    })
    stream._data = data
    if smask:
        mask = EncodedStreamObject()
        mask.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(image.width),
            NameObject("/Height"): NumberObject(image.height),
            NameObject("/ColorSpace"): NameObject("/DeviceGray"),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/Filter"): NameObject("/FlateDecode"),
        })
        mask._data = zlib.compress(b"\xff" * (image.width * image.height), 9)
        stream[NameObject("/SMask")] = writer._add_object(mask)
    return stream


def _pdf_with_images(path: pathlib.Path, specs: list[dict], *,
                     dpi: float = 300.0, pages: int = 1) -> pathlib.Path:
    """Pages carrying each spec's image, sized so the FIRST image sits at
    exactly `dpi` when it covers the page. With `pages` > 1 every page draws
    the very same image objects, as the CV photo does on Deckblatt and
    Lebenslauf."""
    writer = PdfWriter()
    first = specs[0]["image"]
    width, height = first.width / dpi * 72, first.height / dpi * 72
    refs, draw = [], []
    for index, spec in enumerate(specs):
        stream = _image_xobject(writer, spec["image"],
                                lossless=spec.get("lossless", True),
                                smask=spec.get("smask", False))
        if spec.get("image_mask"):
            stream[NameObject("/ImageMask")] = BooleanObject(True)
        if spec.get("decode"):
            stream[NameObject("/Decode")] = ArrayObject(
                [NumberObject(1), NumberObject(0)] * 3
            )
        refs.append((f"/Im{index}", writer._add_object(stream)))
        draw.append(f"q {width} 0 0 {height} 0 0 cm /Im{index} Do Q")
    content = DecodedStreamObject()
    content.set_data("\n".join(draw).encode("ascii"))
    content_ref = writer._add_object(content)
    for _ in range(pages):
        page = writer.add_blank_page(width=width, height=height)
        resources = page.setdefault(NameObject("/Resources"), DictionaryObject())
        xobjects = resources.setdefault(NameObject("/XObject"), DictionaryObject())
        for name, ref in refs:
            xobjects[NameObject(name)] = ref
        page[NameObject("/Contents")] = content_ref
    with path.open("wb") as fh:
        writer.write(fh)
    return path


def _payloads(path: pathlib.Path) -> list[bytes]:
    return [img.data for page in PdfReader(str(path)).pages for img in page.images]


def test_safe_filename_transliterates_and_strips():
    assert pdf.safe_filename("Müller & Söhne GmbH") == "Mueller_Soehne_GmbH"
    assert pdf.safe_filename("  Weiß/AG  ") == "Weiss_AG"
    assert pdf.safe_filename("x" * 100) == "x" * 60


def test_html_to_pdf_renders_with_real_chrome(tmp_path):
    """Real seam: Chrome must exist locally and in CI — a missing browser is
    a red build, not a skip."""
    out = tmp_path / "letter.pdf"
    pdf.html_to_pdf("<h1>Bewerbung Test</h1><p>Absatz</p>", out)
    assert out.exists()
    assert out.read_bytes()[:5] == b"%PDF-"
    assert pdf.page_count(out) == 1


def test_merge_pdfs_concatenates_in_order(tmp_path):
    from pypdf import PdfReader

    a = _blank_pdf(tmp_path / "a.pdf", pages=2)          # A4 pages
    b = tmp_path / "b.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)          # distinctive size
    with b.open("wb") as fh:
        writer.write(fh)

    out = tmp_path / "merged.pdf"
    pdf.merge_pdfs([a, b], out)
    reader = PdfReader(str(out))
    assert len(reader.pages) == 3
    # order pinned by page dimensions: A4, A4, then the tiny page LAST
    widths = [round(float(p.mediabox.width)) for p in reader.pages]
    assert widths == [595, 595, 100]
    assert not out.with_suffix(".pdf.part").exists()  # atomic write cleaned up


def test_html_to_pdf_timeout_and_missing_chrome_raise_pdf_errors(
    tmp_path, monkeypatch
):
    import subprocess as sp

    monkeypatch.setattr(pdf, "find_chrome", lambda: None)
    with pytest.raises(pdf.PdfError, match="not found"):
        pdf.html_to_pdf("<p>x</p>", tmp_path / "a.pdf")

    monkeypatch.setattr(pdf, "find_chrome", lambda: "/usr/bin/true")

    def timing_out(*args, **kwargs):
        raise sp.TimeoutExpired(cmd="chrome", timeout=1)

    monkeypatch.setattr(pdf.subprocess, "run", timing_out)
    with pytest.raises(pdf.PdfError, match="did not finish"):
        pdf.html_to_pdf("<p>x</p>", tmp_path / "b.pdf")


def test_merge_pdfs_fails_loudly_on_broken_part(tmp_path):
    good = _blank_pdf(tmp_path / "good.pdf")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")
    with pytest.raises(pdf.PdfError, match="broken.pdf"):
        pdf.merge_pdfs([good, broken], tmp_path / "out.pdf")


def test_collect_anlagen_sorted_and_pdf_only(tmp_path):
    anlagen = tmp_path / "anlagen"
    anlagen.mkdir()
    _blank_pdf(anlagen / "02_diploma.pdf")
    _blank_pdf(anlagen / "01_zeugnis.pdf")
    (anlagen / "notes.txt").write_text("ignore me")
    got = pdf.collect_anlagen(str(anlagen))
    assert [p.name for p in got] == ["01_zeugnis.pdf", "02_diploma.pdf"]

    assert pdf.collect_anlagen("") == []
    assert pdf.collect_anlagen("   ") == []
    with pytest.raises(pdf.PdfError, match="does not exist"):
        pdf.collect_anlagen(str(tmp_path / "missing"))


def test_compress_pdf_recodes_a_lossless_scan_and_keeps_the_page(tmp_path):
    src = _pdf_with_images(tmp_path / "src.pdf",
                           [{"image": _noisy(1200, 900), "lossless": True}])
    out = tmp_path / "out.pdf"
    assert pdf.compress_pdf(src, out, max_dpi=300, quality=85) == 1
    assert out.stat().st_size < src.stat().st_size / 2
    reader = PdfReader(str(out))
    assert len(reader.pages) == 1
    image = reader.pages[0].images[0]
    assert image.image.size == (1200, 900)  # re-encoded, NOT downsampled
    assert str(image.indirect_reference.get_object()["/Filter"]) == "/DCTDecode"


def test_compress_pdf_leaves_an_already_compressed_scan_byte_identical(tmp_path):
    """The rule that pays for itself: a second JPEG pass over a scan already
    within budget costs generation loss and bought 3-8% on the real Mappe."""
    src = _pdf_with_images(tmp_path / "src.pdf",
                           [{"image": _noisy(1200, 900), "lossless": False}],
                           dpi=200.0)
    out = tmp_path / "out.pdf"
    assert pdf.compress_pdf(src, out, max_dpi=300, quality=85) == 0
    assert _payloads(out) == _payloads(src)


def test_compress_pdf_downsamples_a_scan_above_the_target_dpi(tmp_path):
    src = _pdf_with_images(tmp_path / "src.pdf",
                           [{"image": _noisy(1200, 900), "lossless": False}],
                           dpi=600.0)
    out = tmp_path / "out.pdf"
    assert pdf.compress_pdf(src, out, max_dpi=200, quality=80) == 1
    # 600 dpi -> 200 dpi is a third of the pixels in each direction
    assert PdfReader(str(out)).pages[0].images[0].image.size == (400, 300)


def test_compress_pdf_refuses_images_it_cannot_re_encode_faithfully(tmp_path):
    """Transparency, stencils and /Decode inversions survive a JPEG pass
    badly, and a logo is too small to be worth the loss."""
    specs = [
        {"image": _noisy(1200, 900), "lossless": True, "smask": True},
        {"image": _noisy(1200, 900), "lossless": True, "image_mask": True},
        {"image": _noisy(1200, 900), "lossless": True, "decode": True},
        {"image": _noisy(300, 300), "lossless": True},
    ]
    src = _pdf_with_images(tmp_path / "src.pdf", specs)
    out = tmp_path / "out.pdf"
    assert pdf.compress_pdf(src, out, max_dpi=200, quality=80) == 0
    assert _payloads(out) == _payloads(src)


def test_compress_pdf_shares_one_object_drawn_on_several_pages(tmp_path):
    """The CV photo sits on the Deckblatt AND the Lebenslauf as one object —
    it must be re-encoded once, not re-encoded on top of itself."""
    src = _pdf_with_images(tmp_path / "src.pdf",
                           [{"image": _noisy(1200, 900), "lossless": True}],
                           pages=2)
    out = tmp_path / "out.pdf"
    assert pdf.compress_pdf(src, out, max_dpi=300, quality=85) == 1
    reader = PdfReader(str(out))
    assert len(reader.pages) == 2
    first, second = (p.images[0].indirect_reference for p in reader.pages)
    assert (first.idnum, first.generation) == (second.idnum, second.generation)


def test_compress_to_target_passes_a_file_already_in_budget_through_untouched(
    tmp_path,
):
    src = _pdf_with_images(tmp_path / "src.pdf",
                           [{"image": _noisy(1200, 900), "lossless": True}])
    out = tmp_path / "out.pdf"
    result = pdf.compress_to_target(src, out, target_bytes=10 * 1024 * 1024)
    assert result.applied is False
    assert result.met_target is True
    assert result.dpi is None and result.images_recoded == 0
    assert result.describe() == ""
    assert out.read_bytes() == src.read_bytes()


def test_compress_to_target_stops_at_the_gentlest_rung_that_fits(tmp_path):
    src = _pdf_with_images(tmp_path / "src.pdf",
                           [{"image": _noisy(1200, 900), "lossless": True}])
    out = tmp_path / "out.pdf"
    gentlest = pdf.COMPRESSION_LADDER[0]
    probe = tmp_path / "probe.pdf"
    pdf.compress_pdf(src, probe, max_dpi=gentlest[0], quality=gentlest[1])

    result = pdf.compress_to_target(src, out, target_bytes=probe.stat().st_size)
    assert (result.dpi, result.quality) == gentlest
    assert result.met_target is True
    assert result.size_bytes <= probe.stat().st_size
    assert result.original_bytes == src.stat().st_size
    assert "→" in result.describe()


def test_compress_to_target_never_goes_below_the_quality_floor(tmp_path):
    """An unreachable target must not produce an illegible Zeugnis: the last
    ladder rung is the floor, and the caller is told the target was missed."""
    src = _pdf_with_images(tmp_path / "src.pdf",
                           [{"image": _noisy(1200, 900), "lossless": True}])
    out = tmp_path / "out.pdf"
    result = pdf.compress_to_target(src, out, target_bytes=1024)
    floor_dpi, floor_quality = pdf.COMPRESSION_LADDER[-1]
    assert (result.dpi, result.quality) == (floor_dpi, floor_quality)
    assert result.met_target is False
    assert result.size_bytes > 1024
    assert PdfReader(str(out)).pages[0].images[0].image.size == (
        round(1200 * floor_dpi / 300), round(900 * floor_dpi / 300)
    )


def test_compress_to_target_keeps_the_original_when_nothing_can_be_gained(
    tmp_path,
):
    """Every image is off-limits, so each rung reproduces the input; shipping
    a rewritten-but-no-smaller file would be pure risk."""
    src = _pdf_with_images(
        tmp_path / "src.pdf",
        [{"image": _noisy(1200, 900), "lossless": True, "smask": True}],
    )
    out = tmp_path / "out.pdf"
    result = pdf.compress_to_target(src, out, target_bytes=1024)
    assert result.applied is False
    assert result.met_target is False
    assert result.size_bytes == src.stat().st_size
    assert out.read_bytes() == src.read_bytes()


def test_compress_pdf_leaves_an_undecodable_image_alone(tmp_path):
    """A corrupt image must cost its OWN compression, not the whole page's:
    the healthy scan beside it still has to shrink."""
    src = _pdf_with_images(tmp_path / "src.pdf", [
        {"image": _noisy(1200, 900), "lossless": True},
        {"image": _noisy(1200, 900), "lossless": True},
    ])
    writer = PdfWriter(clone_from=str(src))
    corrupt = writer.pages[0].images[0].indirect_reference.get_object()
    corrupt._data = b"\x00\x01\x02 not a valid flate stream"
    broken = tmp_path / "broken.pdf"
    with broken.open("wb") as fh:
        writer.write(fh)

    out = tmp_path / "out.pdf"
    assert pdf.compress_pdf(broken, out, max_dpi=200, quality=80) == 1
    assert out.stat().st_size < broken.stat().st_size
    assert len(PdfReader(str(out)).pages) == 1


def test_compress_pdf_cleans_up_its_temporary_file(tmp_path):
    src = _pdf_with_images(tmp_path / "src.pdf",
                           [{"image": _noisy(1200, 900), "lossless": True}])
    out = tmp_path / "out.pdf"
    pdf.compress_pdf(src, out, max_dpi=300, quality=85)
    assert not out.with_suffix(".pdf.part").exists()


def test_effective_dpi_accounts_for_page_rotation():
    # A4 portrait carrying a landscape scan, rotated for display
    assert pdf._effective_dpi(2480, 3508, 8.27, 11.69) == pytest.approx(300, abs=1)
    assert pdf._page_size_inches(
        PdfReader(io.BytesIO(_rotated_a4())).pages[0]
    ) == pytest.approx((11.69, 8.27), abs=0.01)


def _rotated_a4() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    page[NameObject("/Rotate")] = NumberObject(90)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
