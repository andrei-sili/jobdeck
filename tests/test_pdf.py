import pathlib

import pytest
from pypdf import PdfWriter

from jobdeck import pdf


def _blank_pdf(path: pathlib.Path, pages: int = 1) -> pathlib.Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)  # A4 points
    with path.open("wb") as fh:
        writer.write(fh)
    return path


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
