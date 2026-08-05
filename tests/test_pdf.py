import pathlib

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, ByteStringObject, NameObject, NumberObject

from jobdeck import pdf


def _blank_pdf(path: pathlib.Path, pages: int = 1) -> pathlib.Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)  # A4 points
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


def test_compress_pdf_recodes_a_lossless_scan_and_keeps_the_page(tmp_path, image_pdf, noisy_image):
    src = image_pdf(tmp_path / "src.pdf",
                    [{"image": noisy_image(1200, 900), "lossless": True}])
    out = tmp_path / "out.pdf"
    assert pdf.compress_pdf(src, out, max_dpi=300, quality=85) == 1
    assert out.stat().st_size < src.stat().st_size / 2
    reader = PdfReader(str(out))
    assert len(reader.pages) == 1
    image = reader.pages[0].images[0]
    assert image.image.size == (1200, 900)  # re-encoded, NOT downsampled
    assert str(image.indirect_reference.get_object()["/Filter"]) == "/DCTDecode"


def test_compress_pdf_leaves_an_already_compressed_scan_byte_identical(
    tmp_path, image_pdf, noisy_image
):
    """The rule that pays for itself: a second JPEG pass over a scan already
    within budget costs generation loss and bought 3-8% on the real Mappe."""
    src = image_pdf(tmp_path / "src.pdf",
                    [{"image": noisy_image(1200, 900), "lossless": False}],
                           dpi=200.0)
    out = tmp_path / "out.pdf"
    assert pdf.compress_pdf(src, out, max_dpi=300, quality=85) == 0
    assert _payloads(out) == _payloads(src)


def test_compress_pdf_downsamples_a_scan_above_the_target_dpi(tmp_path, image_pdf, noisy_image):
    src = image_pdf(tmp_path / "src.pdf",
                    [{"image": noisy_image(1200, 900), "lossless": False}],
                           dpi=600.0)
    out = tmp_path / "out.pdf"
    assert pdf.compress_pdf(src, out, max_dpi=200, quality=80) == 1
    # 600 dpi -> 200 dpi is a third of the pixels in each direction
    assert PdfReader(str(out)).pages[0].images[0].image.size == (400, 300)


def test_compress_pdf_refuses_images_it_cannot_re_encode_faithfully(
    tmp_path, image_pdf, noisy_image
):
    """Transparency, stencils and /Decode inversions survive a JPEG pass
    badly, and a logo is too small to be worth the loss."""
    specs = [
        {"image": noisy_image(1200, 900), "lossless": True, "smask": True},
        {"image": noisy_image(1200, 900), "lossless": True, "image_mask": True},
        {"image": noisy_image(1200, 900), "lossless": True, "decode": True},
        {"image": noisy_image(300, 300), "lossless": True},
    ]
    src = image_pdf(tmp_path / "src.pdf", specs)
    out = tmp_path / "out.pdf"
    assert pdf.compress_pdf(src, out, max_dpi=200, quality=80) == 0
    assert _payloads(out) == _payloads(src)


def test_compress_pdf_refuses_a_colour_key_masked_image(tmp_path, image_pdf, noisy_image):
    """A /Mask array leaves the image itself plain RGB, so the decoded mode
    cannot catch it: re-encoding would drop the mask and paint over whatever
    the mask was hiding."""
    src = image_pdf(
        tmp_path / "src.pdf",
        [{"image": noisy_image(1200, 900), "lossless": True, "colour_key_mask": True}],
    )
    out = tmp_path / "out.pdf"
    assert pdf.compress_pdf(src, out, max_dpi=200, quality=80) == 0
    assert _payloads(out) == _payloads(src)


def test_compress_pdf_refuses_an_image_jpeg_would_make_bigger(
    tmp_path, image_pdf, line_art_image
):
    """JPEG is not a universal win. Line art and text scans are exactly what
    Flate stores well: one measured here grew 17.9x AND gained ringing on
    every edge. The file-level guard cannot see it — a page that doubles is
    invisible beside three that shrink — so the refusal is per image."""
    src = image_pdf(tmp_path / "src.pdf",
                    [{"image": line_art_image(1600, 1200), "lossless": True}])
    out = tmp_path / "out.pdf"
    assert pdf.compress_pdf(src, out, max_dpi=300, quality=85) == 0
    assert _payloads(out) == _payloads(src)
    assert out.stat().st_size <= src.stat().st_size


def test_compress_pdf_screens_the_dictionary_not_the_decoded_mode(
    tmp_path, image_pdf, noisy_image
):
    """pypdf reports a 16-bit scan as a mangled 8-bit "L" and an /Indexed one
    as plain "RGB", so a check on the decoded image lets both through. The
    palette is what keeps an /Indexed file small and its edges sharp; the
    16-bit samples are already wrong by the time Pillow sees them."""
    palette = ArrayObject([
        NameObject("/Indexed"), NameObject("/DeviceRGB"),
        NumberObject(255), ByteStringObject(bytes(range(256)) * 3),
    ])
    specs = [
        {"image": noisy_image(1200, 900), "bits": 16,
         "colourspace": NameObject("/DeviceGray")},
        {"image": noisy_image(1200, 900), "colourspace": palette},
        {"image": noisy_image(1200, 900), "colourspace": NameObject("/DeviceCMYK")},
    ]
    src = image_pdf(tmp_path / "src.pdf", specs)
    out = tmp_path / "out.pdf"
    assert pdf.compress_pdf(src, out, max_dpi=200, quality=80) == 0
    assert _payloads(out) == _payloads(src)


def test_full_chroma_is_kept_for_documents_with_stamps_and_seals(
    tmp_path, image_pdf, noisy_image
):
    """Pillow defaults to 4:2:0, which halves the COLOUR resolution — on a
    certificate that is the red Stempel and the blue signature bleeding at
    their edges. Full chroma costs 153 KB on the real Mappe and leaves it
    inside every budget."""
    assert pdf.JPEG_SUBSAMPLING == 0  # 4:4:4
    src = image_pdf(tmp_path / "src.pdf",
                    [{"image": noisy_image(1200, 900), "lossless": True}])
    out = tmp_path / "out.pdf"
    pdf.compress_pdf(src, out, max_dpi=300, quality=85)

    subsampled = tmp_path / "subsampled.jpg"
    PdfReader(str(out)).pages[0].images[0].image.save(
        subsampled, "JPEG", quality=85, subsampling=2)
    written = PdfReader(str(out)).pages[0].images[0].data
    assert len(written) > subsampled.stat().st_size  # full chroma really costs


def test_compress_pdf_merges_images_that_became_identical(
    tmp_path, image_pdf, noisy_image
):
    """Certificates from one issuer carry the same background, and after
    re-encoding those become byte-identical objects. Merging them is lossless
    and on the real Mappe it was worth more than a whole ladder rung."""
    shared = noisy_image(1200, 900)
    src = image_pdf(tmp_path / "src.pdf", [
        {"image": shared, "lossless": True},
        {"image": shared, "lossless": True},
        {"image": shared, "lossless": True},
    ])
    out = tmp_path / "out.pdf"
    assert pdf.compress_pdf(src, out, max_dpi=300, quality=85) == 3

    images = PdfReader(str(out)).pages[0].images
    assert len(images) == 3  # still drawn three times…
    refs = {(i.indirect_reference.idnum, i.indirect_reference.generation)
            for i in images}
    assert len(refs) == 1  # …but stored once

    separate = tmp_path / "separate.pdf"
    pdf.compress_pdf(
        image_pdf(tmp_path / "distinct.pdf", [
            {"image": noisy_image(1200, 900), "lossless": True},
            {"image": noisy_image(1201, 900), "lossless": True},
            {"image": noisy_image(1202, 900), "lossless": True},
        ]),
        separate, max_dpi=300, quality=85,
    )
    assert out.stat().st_size < separate.stat().st_size / 2


def test_effective_dpi_takes_the_axis_that_cannot_over_downsample():
    """When an image's aspect does not match its page the two axes disagree.
    min() is the reading that cannot cause over-downsampling — staying above
    the quality floor is worth more here than the last kilobyte."""
    assert pdf._effective_dpi(1200, 900, 4.0, 9.0) == pytest.approx(100)
    assert pdf._effective_dpi(1200, 900, 12.0, 3.0) == pytest.approx(100)
    # aspect-matched: both axes agree and the choice cannot show up
    assert pdf._effective_dpi(1200, 900, 4.0, 3.0) == pytest.approx(300)
    assert pdf._effective_dpi(1200, 900, 0, 3.0) == 0.0


def test_compress_to_target_leaves_no_rung_scratch_file(
    tmp_path, image_pdf, noisy_image
):
    src = image_pdf(tmp_path / "src.pdf",
                    [{"image": noisy_image(1200, 900), "lossless": True}])
    out = tmp_path / "out.pdf"
    pdf.compress_to_target(src, out, target_bytes=1024)  # walks every rung
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix != ".pdf") == []
    assert out.exists()


def test_the_compression_ladder_never_offers_a_rung_below_the_floor():
    """200 dpi / q80 is the level Andrei accepted after comparing it with the
    600-dpi original at equal zoom. It is a product decision, not a tunable:
    a rung below it would silently ship a softer Zeugnis."""
    assert pdf.COMPRESSION_LADDER[-1] == (200, 80)
    assert all(dpi >= 200 and quality >= 80
               for dpi, quality in pdf.COMPRESSION_LADDER)
    assert list(pdf.COMPRESSION_LADDER) == sorted(
        pdf.COMPRESSION_LADDER, reverse=True
    )  # best quality first, so the gentlest rung that fits wins


def test_compress_pdf_shares_one_object_drawn_on_several_pages(tmp_path, image_pdf, noisy_image):
    """The CV photo sits on the Deckblatt AND the Lebenslauf as one object —
    it must be re-encoded once, not re-encoded on top of itself."""
    src = image_pdf(tmp_path / "src.pdf",
                    [{"image": noisy_image(1200, 900), "lossless": True}],
                           pages=2)
    out = tmp_path / "out.pdf"
    assert pdf.compress_pdf(src, out, max_dpi=300, quality=85) == 1
    reader = PdfReader(str(out))
    assert len(reader.pages) == 2
    first, second = (p.images[0].indirect_reference for p in reader.pages)
    assert (first.idnum, first.generation) == (second.idnum, second.generation)


def test_compress_to_target_passes_a_file_already_in_budget_through_untouched(
    tmp_path, image_pdf, noisy_image
):
    src = image_pdf(tmp_path / "src.pdf",
                    [{"image": noisy_image(1200, 900), "lossless": True}])
    out = tmp_path / "out.pdf"
    result = pdf.compress_to_target(src, out, target_bytes=10 * 1024 * 1024)
    assert result.applied is False
    assert result.met_target is True
    assert result.dpi is None and result.images_recoded == 0
    assert result.describe() == ""
    assert out.read_bytes() == src.read_bytes()


def test_compress_to_target_stops_at_the_gentlest_rung_that_fits(tmp_path, image_pdf, noisy_image):
    src = image_pdf(tmp_path / "src.pdf",
                    [{"image": noisy_image(1200, 900), "lossless": True}])
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


def test_compress_to_target_never_goes_below_the_quality_floor(tmp_path, image_pdf, noisy_image):
    """An unreachable target must not produce an illegible Zeugnis: the last
    ladder rung is the floor, and the caller is told the target was missed."""
    src = image_pdf(tmp_path / "src.pdf",
                    [{"image": noisy_image(1200, 900), "lossless": True}])
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
    tmp_path, image_pdf, noisy_image
):
    """Every image is off-limits, so each rung reproduces the input; shipping
    a rewritten-but-no-smaller file would be pure risk."""
    src = image_pdf(
        tmp_path / "src.pdf",
        [{"image": noisy_image(1200, 900), "lossless": True, "smask": True}],
    )
    out = tmp_path / "out.pdf"
    result = pdf.compress_to_target(src, out, target_bytes=1024)
    assert result.applied is False
    assert result.met_target is False
    assert result.size_bytes == src.stat().st_size
    assert out.read_bytes() == src.read_bytes()


def test_compress_pdf_leaves_an_undecodable_image_alone(tmp_path, image_pdf, noisy_image):
    """A corrupt image must cost its OWN compression, not the whole page's:
    the healthy scan beside it still has to shrink."""
    src = image_pdf(tmp_path / "src.pdf", [
        {"image": noisy_image(1200, 900), "lossless": True},
        {"image": noisy_image(1200, 900), "lossless": True},
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


def test_compress_pdf_cleans_up_its_temporary_file(tmp_path, image_pdf, noisy_image):
    src = image_pdf(tmp_path / "src.pdf",
                    [{"image": noisy_image(1200, 900), "lossless": True}])
    out = tmp_path / "out.pdf"
    pdf.compress_pdf(src, out, max_dpi=300, quality=85)
    assert not out.with_suffix(".pdf.part").exists()


def test_display_rotation_does_not_change_the_measured_resolution(
    tmp_path, image_pdf, noisy_image
):
    """/Rotate turns the page for the READER; the mediabox and the matrix the
    image is drawn with stay in unrotated space. Swapping the axes read a
    true 300 dpi scan as 424 and, at the floor rung, would have downsampled a
    200 dpi scan to 141 — through the limit this feature promises to hold."""
    scan = noisy_image(1200, 900)
    upright = image_pdf(tmp_path / "upright.pdf", [{"image": scan}], dpi=300.0)
    turned = image_pdf(tmp_path / "turned.pdf", [{"image": scan}], dpi=300.0)
    writer = PdfWriter(clone_from=str(turned))
    writer.pages[0][NameObject("/Rotate")] = NumberObject(90)
    with turned.open("wb") as fh:
        writer.write(fh)

    def measured(path):
        page = PdfReader(str(path)).pages[0]
        w_in, h_in = pdf._page_size_inches(page)
        return pdf._effective_dpi(1200, 900, w_in, h_in)

    assert measured(upright) == pytest.approx(300, abs=1)
    assert measured(turned) == pytest.approx(measured(upright), abs=0.01)

    # …and it survives the whole pipeline: a 300 dpi scan on a rotated page
    # is NOT downsampled when the budget is 300 dpi.
    out = tmp_path / "out.pdf"
    pdf.compress_pdf(turned, out, max_dpi=300, quality=85)
    assert PdfReader(str(out)).pages[0].images[0].image.size == (1200, 900)
