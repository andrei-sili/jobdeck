"""The Mappe measured before it is sent: the stack, the letter head, the build."""

import pathlib

import pytest
from pypdf import PdfWriter

from jobdeck import db, pdf
from jobdeck.services import mappe, unterlagen

# Generic template with the full token contract — no personal data.
TEMPLATE = """\
<div>
  <div>{{FIRMA}}<br>{{ANSPRECHPARTNER}}<br>{{STRASSE}}<br>{{PLZ_ORT}}</div>
  <div>{{ORT}}, {{DATUM}}</div>
  <h2>{{BETREFF}}</h2>
  {{ANSCHREIBEN_BODY}}
</div>
"""


def _blank_pdf(path: pathlib.Path, pages: int = 1) -> pathlib.Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    with path.open("wb") as fh:
        writer.write(fh)
    return path


def _setup(con, data_dir, *, with_anlagen=True, **overrides):
    job_id = db.insert_job_if_new(con, {
        "source": "arbeitsagentur", "external_id": "REF-77",
        "title": "Python Entwickler (m/w/d)", "company": "Neue Firma GmbH",
        "description": "desc",
    })
    db.set_job_contacts(con, job_id, {
        "ansprechpartner": "Frau Weber", "contact_strasse": "Weg 1",
        "contact_plz_ort": "10115 Berlin", "refnr": "K-17",
    })
    template_file = data_dir / "template.html"
    template_file.write_text(TEMPLATE, encoding="utf-8")
    settings = {
        "applicant_name": "Erika Muster",
        "applicant_ort": "Musterstadt",
        "template_path": str(template_file),
    }
    if with_anlagen:
        anlagen = data_dir / "anlagen"
        anlagen.mkdir()
        _blank_pdf(anlagen / "01_zeugnis.pdf", pages=2)
        _blank_pdf(anlagen / "02_zertifikat.pdf", pages=1)
        settings["anlagen_dir"] = str(anlagen)
    settings.update(overrides)
    for key, value in settings.items():
        db.set_setting(con, key, value)
    con.commit()
    return job_id


# --------------------------------------------------------------------------
# The stack
# --------------------------------------------------------------------------
def test_the_stack_is_read_from_the_files_in_merge_order(con, data_dir):
    _setup(con, data_dir)
    parts, error = unterlagen.anlagen_parts(
        db.get_setting(con, "anlagen_dir", ""))
    assert error == ""
    assert [p.label for p in parts] == ["01_zeugnis", "02_zertifikat"]
    assert [p.pages for p in parts] == [2, 1]
    assert all(p.size_bytes > 0 for p in parts)


def test_an_unreadable_anlage_is_named_and_the_rest_still_read(con, data_dir):
    """One torn certificate must not blank the screen — naming it here is
    the point, because the alternative is discovering it at send time."""
    _setup(con, data_dir)
    folder = pathlib.Path(db.get_setting(con, "anlagen_dir", ""))
    (folder / "03_kaputt.pdf").write_bytes(b"not a pdf at all")

    parts, error = unterlagen.anlagen_parts(str(folder))
    assert error == ""
    assert [p.label for p in parts] == ["01_zeugnis", "02_zertifikat", "03_kaputt"]
    broken = parts[-1]
    assert broken.pages == 0
    assert broken.error.startswith("nicht lesbar")
    assert parts[0].error == "" and parts[0].pages == 2


def test_a_missing_anlagen_folder_is_reported_not_raised(con, data_dir):
    parts, error = unterlagen.anlagen_parts(str(data_dir / "gibt-es-nicht"))
    assert parts == []
    assert "does not exist" in error


def test_no_anlagen_folder_configured_is_not_an_error(con, data_dir):
    assert unterlagen.anlagen_parts("") == ([], "")


def test_page_numbers_run_through_the_whole_stack(con, data_dir):
    """The page a part starts on is what makes the stack a stack — and it is
    exact, because merging preserves both order and page count."""
    parts = unterlagen._numbered([
        unterlagen.Part(label="Vorlage", pages=3, size_bytes=0),
        unterlagen.Part(label="01_zeugnis", pages=2, size_bytes=0),
        unterlagen.Part(label="02_zertifikat", pages=1, size_bytes=0),
    ])
    assert [(p.first_page, p.last_page) for p in parts] == [(1, 3), (4, 5), (6, 6)]
    assert all(p.placed for p in parts)


def test_nothing_after_an_unmeasured_part_gets_a_page_number(con, data_dir):
    """Found in the running app: before the first build the letter's length is
    unknown, and the Zeugnis was announced as pages 1–2 when it really lands
    on 4–5. Numbering through the gap is worse than not numbering — a page
    number is checked against a printout."""
    parts = unterlagen._numbered([
        unterlagen.Part(label="Vorlage", pages=0, size_bytes=0),
        unterlagen.Part(label="01_zeugnis", pages=2, size_bytes=0),
        unterlagen.Part(label="02_zertifikat", pages=1, size_bytes=0),
    ])
    assert parts[0].first_page == 1, "the first part is still where it starts"
    assert [p.placed for p in parts] == [True, False, False]
    assert [p.first_page for p in parts[1:]] == [0, 0]


def test_a_torn_anlage_also_unplaces_what_follows_it(con, data_dir):
    """Same rule, other cause: the pages of a file that cannot be read are
    not zero, they are unknown."""
    parts = unterlagen._numbered([
        unterlagen.Part(label="Vorlage", pages=3, size_bytes=0),
        unterlagen.Part(label="01_kaputt", pages=0, size_bytes=0,
                        error="nicht lesbar"),
        unterlagen.Part(label="02_zertifikat", pages=1, size_bytes=0),
    ])
    assert [p.first_page for p in parts] == [1, 4, 0]
    assert [p.placed for p in parts] == [True, True, False]


# --------------------------------------------------------------------------
# The letter head
# --------------------------------------------------------------------------
def test_the_preview_uses_the_same_values_as_a_real_mappe(con, data_dir):
    """The preview's promise is that a field it shows as filled cannot be
    empty in the PDF. It keeps that by deriving the values in one place."""
    job_id = _setup(con, data_dir)
    view = unterlagen.preview(con, job_id)
    job = db.get_job(con, job_id)
    expected = mappe.letter_values(job, None, "Erika Muster", "Musterstadt")

    assert view["values"] == expected
    assert view["values"]["firma"] == "Neue Firma GmbH"
    assert view["values"]["ansprechpartner"] == "Frau Weber"
    assert view["values"]["ort"] == "Musterstadt"
    assert "K-17" in view["values"]["betreff"]
    assert view["missing"] == []


def test_an_empty_field_is_named_together_with_the_reason(con, data_dir):
    job_id = _setup(con, data_dir)
    con.execute("UPDATE jobs SET ansprechpartner='', contact_strasse='', "
                "contact_plz_ort='', work_strasse='', work_plz_ort='' "
                "WHERE id=?", (job_id,))
    con.commit()

    missing = unterlagen.preview(con, job_id)["missing"]
    assert [m["key"] for m in missing] == ["ansprechpartner", "strasse", "plz_ort"]
    assert "Sehr geehrte Damen und Herren" in missing[0]["why"]


def test_an_unset_applicant_ort_is_named_as_a_settings_problem(con, data_dir):
    """The reason matters more than the fact: this one he can fix in a click,
    while a posting naming no Ansprechpartner is nothing he can do about."""
    job_id = _setup(con, data_dir, applicant_ort="")
    missing = unterlagen.preview(con, job_id)["missing"]
    reason = next(m["why"] for m in missing if m["key"] == "ort")
    assert "Einstellungen" in reason


def test_the_preview_without_a_posting_says_so_rather_than_inventing_one(
        con, data_dir):
    _setup(con, data_dir)
    view = unterlagen.preview(con, None)
    assert view == {"job": None, "values": {}, "missing": []}
    assert unterlagen.preview(con, 999999)["job"] is None


# --------------------------------------------------------------------------
# The signature
# --------------------------------------------------------------------------
def test_the_signature_sees_a_new_anlage_appearing_on_disk(con, data_dir):
    """No table signature can see a file being added, and renaming files is
    how the order of the stack is set."""
    _setup(con, data_dir)
    before = unterlagen.signature(con, None)
    _blank_pdf(pathlib.Path(db.get_setting(con, "anlagen_dir", "")) / "03_neu.pdf")
    assert unterlagen.signature(con, None) != before


def test_the_signature_sees_an_anlage_being_renamed(con, data_dir):
    _setup(con, data_dir)
    folder = pathlib.Path(db.get_setting(con, "anlagen_dir", ""))
    before = unterlagen.signature(con, None)
    (folder / "01_zeugnis.pdf").rename(folder / "04_zeugnis.pdf")
    assert unterlagen.signature(con, None) != before


def test_the_signature_sees_the_template_being_repointed(con, data_dir):
    _setup(con, data_dir)
    before = unterlagen.signature(con, None)
    db.set_setting(con, "template_path", str(data_dir / "andere.html"))
    con.commit()
    assert unterlagen.signature(con, None) != before


def test_the_signature_sees_the_register_change(con, data_dir):
    _setup(con, data_dir)
    before = unterlagen.signature(con, None)
    db.add_claim(con, {"fact": "Java"})
    assert unterlagen.signature(con, None) != before


def test_the_signature_sees_the_previewed_posting_gain_a_contact(con, data_dir):
    """Contact resolution runs in the background and fills exactly the fields
    the preview reports as missing."""
    job_id = _setup(con, data_dir)
    con.execute("UPDATE jobs SET ansprechpartner='' WHERE id=?", (job_id,))
    con.commit()
    before = unterlagen.signature(con, job_id)
    db.set_job_contacts(con, job_id, {"ansprechpartner": "Herr Klein"})
    con.commit()
    assert unterlagen.signature(con, job_id) != before


# --------------------------------------------------------------------------
# The build
# --------------------------------------------------------------------------
@pytest.mark.skipif(pdf.find_chrome() is None, reason="headless Chrome missing")
async def test_building_the_specimen_produces_the_whole_stack(con, data_dir):
    job_id = _setup(con, data_dir)
    result = await unterlagen.build(job_id)

    assert result["ok"], result["error"]
    built = pathlib.Path(result["pdf_path"])
    assert built == unterlagen.specimen_path()
    assert built.is_file()
    # the letter's own pages plus the three Anlagen pages
    assert result["pages"] >= 4
    assert result["size_bytes"] > 0


@pytest.mark.skipif(pdf.find_chrome() is None, reason="headless Chrome missing")
async def test_the_specimen_says_in_its_own_text_that_it_is_one(con, data_dir):
    """A specimen that reads like a real application is one somebody will
    eventually mistake for one — including the person who built it."""
    job_id = _setup(con, data_dir)
    captured = {}

    def capture(template_html, values):
        captured.update(values)
        return "<div>x</div>"

    original = unterlagen.templates.render_letter
    unterlagen.templates.render_letter = capture
    try:
        await unterlagen.build(job_id)
    finally:
        unterlagen.templates.render_letter = original

    assert captured["anschreiben_body"] == unterlagen.SPECIMEN_BODY
    assert "Musterseite" in captured["anschreiben_body"]
    # everything else is the real letter head, not a placeholder
    assert captured["firma"] == "Neue Firma GmbH"
    assert captured["ansprechpartner"] == "Frau Weber"


@pytest.mark.skipif(pdf.find_chrome() is None, reason="headless Chrome missing")
async def test_inspect_reads_the_built_specimen_back_from_its_own_file(
        con, data_dir):
    """The artifact is the cache. Page counts kept anywhere else could
    disagree with the PDF the user is about to open."""
    job_id = _setup(con, data_dir)
    before = await unterlagen.inspect(job_id)
    assert before["specimen"]["built"] is False
    assert before["specimen"]["letter_pages"] == 0

    built = await unterlagen.build(job_id)
    after = await unterlagen.inspect(job_id)

    assert after["specimen"]["built"] is True
    assert after["specimen"]["pages"] == built["pages"]
    assert after["specimen"]["size_bytes"] == built["size_bytes"]
    # the letter is what the specimen has beyond the three Anlagen pages
    assert after["specimen"]["letter_pages"] == built["pages"] - 3
    assert after["parts"][0].pages == after["specimen"]["letter_pages"]


async def test_a_build_without_a_posting_refuses_and_says_why(con, data_dir):
    _setup(con, data_dir)
    result = await unterlagen.build(None)
    assert result["ok"] is False
    assert "no posting" in result["error"]


async def test_a_build_without_a_template_refuses_and_says_why(con, data_dir):
    job_id = _setup(con, data_dir, template_path="")
    result = await unterlagen.build(job_id)
    assert result["ok"] is False
    assert "template path" in result["error"]


async def test_a_build_with_a_template_that_is_gone_names_the_path(con, data_dir):
    job_id = _setup(con, data_dir,
                    template_path=str(data_dir / "weg.html"))
    result = await unterlagen.build(job_id)
    assert result["ok"] is False
    assert "weg.html" in result["error"]


async def test_inspect_names_the_missing_template_in_the_stack(con, data_dir):
    job_id = _setup(con, data_dir, template_path=str(data_dir / "weg.html"))
    view = await unterlagen.inspect(job_id)
    assert view["parts"][0].label == unterlagen.TEMPLATE_LABEL
    assert "Vorlage fehlt" in view["parts"][0].error


async def test_inspect_states_both_budgets_and_the_german_ceiling(con, data_dir):
    job_id = _setup(con, data_dir)
    view = await unterlagen.inspect(job_id)
    assert view["target_email_bytes"] == int(mappe.DEFAULT_TARGET_MB * 1024 * 1024)
    assert view["target_portal_bytes"] == int(
        mappe.DEFAULT_PORTAL_TARGET_MB * 1024 * 1024)
    assert view["target_portal_bytes"] < view["target_email_bytes"]
    assert view["max_bytes"] == pdf.MAX_MAPPE_BYTES
