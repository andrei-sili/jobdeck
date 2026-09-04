"""What a parser makes of a PDF, measured on real files rendered through the
same Chrome path the build uses — and on one hand-written Type 3 PDF, the
shape a variable webfont takes when Chrome prints it."""


from pypdf import PdfWriter

from jobdeck import pdf
from jobdeck.services import atscheck

CV_HTML = """<html><body style="font-family:sans-serif">
<h1>Erika Muster</h1>
<p>Musterweg 1 · 12345 Musterstadt · +49 170 1234567 · erika@example.org</p>
<h2>Profil</h2><p>Fachinformatikerin, ab sofort verfügbar.</p>
<h2>Berufserfahrung</h2><p>Praktikum Softwareentwicklung · 01/2025 – 07/2025</p>
<p>Backend-Entwicklung mit Python und Django, REST-Endpunkte, API-Testing.</p>
<h2>Ausbildung</h2><p>Ausbildung zur Fachinformatikerin · 08/2022 – 07/2025</p>
<h2>Kenntnisse</h2><p>Python, Django, PostgreSQL, Docker, Git</p>
<h2>Sprachen</h2><p>Deutsch (C1) · Englisch (gut)</p>
</body></html>"""

# A minimal PDF whose only font is Type 3: one glyph drawn as a filled box,
# the text "AB" set in it. This is what a variable webfont becomes.
TYPE3_PDF = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200]
  /Resources << /Font << /F1 4 0 R >> >> /Contents 6 0 R >> endobj
4 0 obj << /Type /Font /Subtype /Type3 /FontBBox [0 0 750 750]
  /FontMatrix [0.001 0 0 0.001 0 0] /CharProcs 5 0 R
  /Encoding << /Type /Encoding /Differences [65 /square 66 /square] >>
  /FirstChar 65 /LastChar 66 /Widths [800 800] /Resources << >> >> endobj
5 0 obj << /square 7 0 R >> endobj
6 0 obj << /Length 35 >> stream
BT /F1 24 Tf 20 100 Td (AB) Tj ET
endstream endobj
7 0 obj << /Length 39 >> stream
800 0 0 0 750 750 d1 0 0 750 750 re f
endstream endobj
trailer << /Root 1 0 R >>
startxref
0
%%EOF
"""


def test_a_clean_one_column_cv_passes_every_check(tmp_path):
    out = tmp_path / "cv.pdf"
    pdf.html_to_pdf(CV_HTML, out)

    report = atscheck.inspect(out, budget_bytes=2 * 1024 * 1024)

    assert report.error == ""
    assert report.passed, [c.text for c in report.checks if not c.ok]
    assert report.type3_fonts == 0 and report.fonts >= 1
    assert {"Berufserfahrung", "Ausbildung", "Kenntnisse"} <= set(report.headings)
    assert report.pages == 1


def test_a_scan_without_a_text_layer_is_named_as_such(tmp_path):
    out = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with out.open("wb") as fh:
        writer.write(fh)

    report = atscheck.inspect(out)

    assert not report.passed
    assert any("Kaum Text" in c.text for c in report.checks if not c.ok)


def test_a_type3_font_is_reported_by_name(tmp_path):
    out = tmp_path / "type3.pdf"
    out.write_bytes(TYPE3_PDF)

    report = atscheck.inspect(out, expect_headings=False)

    assert report.type3_fonts == 1
    assert any("Type 3" in c.text for c in report.checks if not c.ok)


def test_a_letter_spaced_heading_is_a_finding_not_a_heading():
    """pdfminer-style extraction turns letter-spacing into
    'B E R U F S E R F A H R U N G'. The heading is still recognised — and
    the spacing is reported, because a parser that does not fold it back
    sees no heading at all."""
    folded = atscheck._unspace("B E R U F S E R F A H R U N G IT\nPython")
    assert folded.startswith("BERUFSERFAHRUNG IT")
    assert atscheck._SPACED_RE.search("B E R U F S E R F A H R U N G")
    assert not atscheck._SPACED_RE.search("BERUFSERFAHRUNG IT · Python")


def test_a_long_url_is_not_glued_text():
    """The first live run flagged a project link as words glued together."""
    assert not atscheck._GLUED_RE.search(
        "github.com/andrei-sili/ecommerce-microservices · pm.example.org")
    assert atscheck._GLUED_RE.search(
        "BackendEntwicklungeinerWebanwendungDatenmodelleSerializerundRESTEndpunkte")


def test_a_letter_is_not_marked_down_for_having_no_cv_headings(tmp_path):
    out = tmp_path / "brief.pdf"
    pdf.html_to_pdf("<html><body><p>Sehr geehrte Frau Muster, " + "x " * 150
                    + "erika@example.org +49 170 1234567</p></body></html>", out)

    report = atscheck.inspect(out, expect_headings=False)

    assert report.passed, [c.text for c in report.checks if not c.ok]


def test_a_missing_or_torn_file_is_an_error_not_a_crash(tmp_path):
    assert atscheck.inspect(tmp_path / "nein.pdf").error == "Datei nicht gefunden"
    torn = tmp_path / "torn.pdf"
    torn.write_bytes(b"%PDF-1.4 nothing")
    report = atscheck.inspect(torn)
    assert report.error.startswith("PDF nicht lesbar")
    assert not report.passed


def test_the_portal_budget_is_a_check_when_given(tmp_path):
    out = tmp_path / "cv.pdf"
    pdf.html_to_pdf(CV_HTML, out)
    over = atscheck.inspect(out, budget_bytes=100)
    assert any("über dem Portal-Budget" in c.text for c in over.checks if not c.ok)
    assert atscheck.inspect(out).checks[-1].text != "über"  # no budget, no check
    assert not any("Budget" in c.text for c in atscheck.inspect(out).checks)


def test_first_pages_limits_the_text_checks_but_not_the_page_count(tmp_path):
    """A merged Mappe: the CV in front, a scanned certificate behind it. The
    scan's own text is not the CV's typography — its letter-spaced title must
    not be reported as a heading defect — but the file's size and page count
    are still the whole file's."""
    cv = tmp_path / "cv.pdf"
    pdf.html_to_pdf(CV_HTML, cv)
    scan = tmp_path / "scan.pdf"
    pdf.html_to_pdf("<html><body><p style='letter-spacing:.4em'>CERTIFICATE OF "
                    "COMPLETION</p></body></html>", scan)
    merged = tmp_path / "mappe.pdf"
    pdf.merge_pdfs([cv, scan], merged)

    whole = atscheck.inspect(merged)
    front = atscheck.inspect(merged, first_pages=1)

    assert whole.pages == front.pages == 2
    assert not whole.passed and any("Buchstabe" in c.text for c in whole.checks
                                    if not c.ok)
    assert front.passed, [c.text for c in front.checks if not c.ok]
