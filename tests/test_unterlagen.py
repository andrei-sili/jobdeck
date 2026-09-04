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
    assert "Keine Anzeige" in result["error"]


async def test_a_build_without_a_template_refuses_and_says_why(con, data_dir):
    job_id = _setup(con, data_dir, template_path="")
    result = await unterlagen.build(job_id)
    assert result["ok"] is False
    assert "Briefvorlage" in result["error"]


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


# --------------------------------------------------------------------------
# What the build remembers, and what goes stale under it
# --------------------------------------------------------------------------
def _fake_specimen(data_dir, pages: int) -> pathlib.Path:
    """A built specimen, without paying for a Chrome render."""
    path = unterlagen.specimen_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return _blank_pdf(path, pages=pages)


def _remember_build(con, letter_pages: int, parts) -> None:
    db.set_setting(con, unterlagen.LETTER_PAGES_SETTING, str(letter_pages))
    db.set_setting(con, unterlagen.ANLAGEN_SETTING,
                   unterlagen._anlagen_stamp(parts))
    con.commit()


def test_a_new_anlage_does_not_silently_rewrite_the_letters_page_count(
        con, data_dir):
    """The panel's worst possible lie, and the reason the letter's length is
    remembered instead of subtracted. With a 3-page letter and 3 pages of
    Anlagen, dropping in a 2-page certificate used to re-attribute its pages
    to the letter: the Zeugnis moved from 4–5 to 2–3 and the total still read
    6 while the real Mappe would be 8."""
    _setup(con, data_dir)
    parts, _ = unterlagen.anlagen_parts(db.get_setting(con, "anlagen_dir", ""))
    _fake_specimen(data_dir, pages=6)
    _remember_build(con, 3, parts)

    view = unterlagen.read(con, None)
    assert view["specimen"]["letter_pages"] == 3
    assert [(p.label, p.first_page) for p in view["parts"]] == [
        (unterlagen.TEMPLATE_LABEL, 1), ("01_zeugnis", 4), ("02_zertifikat", 6)]
    assert view["specimen"]["stale"] is False

    _blank_pdf(pathlib.Path(db.get_setting(con, "anlagen_dir", "")) / "03_neu.pdf",
               pages=2)
    after = unterlagen.read(con, None)

    assert after["specimen"]["letter_pages"] == 3, \
        "the new Anlage's pages were attributed to the letter"
    assert [(p.label, p.first_page) for p in after["parts"]] == [
        (unterlagen.TEMPLATE_LABEL, 1), ("01_zeugnis", 4), ("02_zertifikat", 6),
        ("03_neu", 7)]
    assert after["specimen"]["pages"] == 8, "the total came off the stale file"
    assert after["specimen"]["stale"] is True


def test_a_deleted_anlage_does_not_inflate_the_letter_either(con, data_dir):
    _setup(con, data_dir)
    parts, _ = unterlagen.anlagen_parts(db.get_setting(con, "anlagen_dir", ""))
    _fake_specimen(data_dir, pages=6)
    _remember_build(con, 3, parts)

    (pathlib.Path(db.get_setting(con, "anlagen_dir", "")) / "01_zeugnis.pdf").unlink()
    view = unterlagen.read(con, None)

    assert view["specimen"]["letter_pages"] == 3
    assert view["specimen"]["pages"] == 4
    assert view["specimen"]["stale"] is True


def test_a_missing_anlagen_folder_does_not_attribute_the_whole_mappe_to_the_letter(
        con, data_dir):
    """The degenerate case: with no readable Anlagen the subtraction gave the
    letter every page in the file, printed beside the warning saying the
    folder does not exist."""
    _setup(con, data_dir)
    parts, _ = unterlagen.anlagen_parts(db.get_setting(con, "anlagen_dir", ""))
    _fake_specimen(data_dir, pages=6)
    _remember_build(con, 3, parts)

    db.set_setting(con, "anlagen_dir", str(data_dir / "weg"))
    con.commit()
    view = unterlagen.read(con, None)

    assert view["specimen"]["letter_pages"] == 3, \
        "the Anlagen's pages were charged to the letter"
    assert view["anlagen_error"]


def test_a_torn_anlage_keeps_its_pages_out_of_the_letters_count(con, data_dir):
    _setup(con, data_dir)
    parts, _ = unterlagen.anlagen_parts(db.get_setting(con, "anlagen_dir", ""))
    _fake_specimen(data_dir, pages=6)
    _remember_build(con, 3, parts)

    folder = pathlib.Path(db.get_setting(con, "anlagen_dir", ""))
    (folder / "01_zeugnis.pdf").write_bytes(b"torn")
    view = unterlagen.read(con, None)

    assert view["specimen"]["letter_pages"] == 3
    assert view["parts"][1].error.startswith("nicht lesbar")


def test_the_stack_is_not_stale_when_only_an_anlage_is_re_saved(con, data_dir):
    """Names and page counts, not mtimes: re-saving an unchanged certificate
    must not tell him his measurements have expired."""
    _setup(con, data_dir)
    folder = pathlib.Path(db.get_setting(con, "anlagen_dir", ""))
    parts, _ = unterlagen.anlagen_parts(str(folder))
    _fake_specimen(data_dir, pages=6)
    _remember_build(con, 3, parts)

    _blank_pdf(folder / "01_zeugnis.pdf", pages=2)  # same name, same length
    assert unterlagen.read(con, None)["specimen"]["stale"] is False


@pytest.mark.skipif(pdf.find_chrome() is None, reason="headless Chrome missing")
async def test_a_build_writes_down_what_it_measured(con, data_dir):
    job_id = _setup(con, data_dir)
    result = await unterlagen.build(job_id)

    assert result["ok"], result["error"]
    assert result["letter_pages"] >= 1
    assert db.get_setting(con, unterlagen.LETTER_PAGES_SETTING, "") == \
        str(result["letter_pages"])
    assert db.get_setting(con, unterlagen.ANLAGEN_SETTING, "") == \
        "01_zeugnis:2|02_zertifikat:1"
    assert unterlagen.read(con, job_id)["specimen"]["stale"] is False


@pytest.mark.skipif(pdf.find_chrome() is None, reason="headless Chrome missing")
async def test_the_compression_facts_survive_into_the_screen(con, data_dir):
    """The "verlustfrei von 3,7 MB" line is the only evidence the lossless
    rung is doing its job on his real Anlagen."""
    job_id = _setup(con, data_dir)
    await unterlagen.build(job_id)

    view = unterlagen.read(con, job_id)
    assert view["shrunk_from_bytes"] == unterlagen._int_setting(
        db.get_setting(con, unterlagen.BEFORE_SETTING, ""))
    assert view["lossless"] is (
        db.get_setting(con, unterlagen.LOSSLESS_SETTING, "") == "1")
    assert db.get_setting(con, unterlagen.LOSSLESS_SETTING, "") in ("0", "1")


@pytest.mark.parametrize("raw,expected", [
    ("3670016", 3670016), ("", 0), ("nonsense", 0), ("-5", 0), (" 42 ", 42),
])
def test_a_stored_byte_count_is_screened_not_trusted(raw, expected):
    """app_settings lives in a directory the user is invited to edit."""
    assert unterlagen._int_setting(raw) == expected


# --------------------------------------------------------------------------
# More of what the signature has to see
# --------------------------------------------------------------------------
def test_the_signature_sees_the_search_rules_the_panel_prints(con, data_dir):
    """`stale_age_days` is not decoration: it is also the age filter that
    decides which posting the whole letter-head panel is built from."""
    _setup(con, data_dir)
    for key, value in (("global_hard_tags", "Keine Ausbildung"),
                       ("stale_age_days", "5")):
        before = unterlagen.signature(con, None)
        db.set_setting(con, key, value)
        con.commit()
        assert unterlagen.signature(con, None) != before, f"{key} is unsigned"


def test_the_signature_sees_a_profile_being_switched_off(con, data_dir):
    """`profiles_signature` counts rows and poll stamps; it cannot see
    `active`, and the panel prints "(inaktiv)"."""
    _setup(con, data_dir)
    profile_id = db.add_profile(con, {"name": "P", "keywords": "python"})
    con.commit()
    before = unterlagen.signature(con, None)
    db.update_profile(con, profile_id, {"name": "P", "keywords": "python",
                                        "active": 0})
    con.commit()
    assert unterlagen.signature(con, None) != before


def test_the_signature_sees_the_specimen_itself_appear_and_vanish(con, data_dir):
    """The specimen file IS the screen's cache of the built Mappe — the total,
    the weight and whether "Ansehen" leads anywhere all come from it."""
    _setup(con, data_dir)
    before = unterlagen.signature(con, None)
    _fake_specimen(data_dir, pages=6)
    built = unterlagen.signature(con, None)
    assert built != before

    unterlagen.specimen_path().unlink()
    assert unterlagen.signature(con, None) != built


def test_the_signature_sees_the_template_file_itself(con, data_dir):
    """Only the PATH was signed. Putting the template where the setting
    already points left the screen saying "Vorlage fehlt" for the life of the
    page; moving it away left it describing a Mappe that can no longer build."""
    _setup(con, data_dir, template_path=str(data_dir / "spaeter.html"))
    before = unterlagen.signature(con, None)
    assert "Vorlage fehlt" in unterlagen.read(con, None)["parts"][0].error

    (data_dir / "spaeter.html").write_text(TEMPLATE, encoding="utf-8")
    after = unterlagen.signature(con, None)
    assert after != before
    assert unterlagen.read(con, None)["parts"][0].error == ""

    (data_dir / "spaeter.html").write_text(TEMPLATE + "<p>x</p>", encoding="utf-8")
    assert unterlagen.signature(con, None) != after, "an edit is invisible"


def test_the_signature_sees_the_profile_file_being_edited(con, data_dir):
    """The coverage line names sections read from profile.md, and he edits
    that file outside this app. Without its fingerprint the screen would go
    on naming a section he had renamed, or reporting a gap he had filled —
    the same reason the template's CONTENT is signed and not only its path."""
    from jobdeck import config

    _setup(con, data_dir)
    config.PROFILE_PATH.write_text("## Zertifikate\nEins\n", encoding="utf-8")
    before = unterlagen.signature(con, None)

    config.PROFILE_PATH.write_text("## Zertifikate\n## Sprachen\n",
                                   encoding="utf-8")
    assert unterlagen.signature(con, None) != before, "an edit is invisible"


# --------------------------------------------------------------------------
# The reason a field is empty
# --------------------------------------------------------------------------
def test_arbeitnehmeruberlassung_is_named_as_the_reason_for_the_empty_address(
        con, data_dir):
    """The board DOES state an address here; it is refused on purpose, because
    it is the client's site and not the recipient. Blaming "weder Anzeige noch
    Board" sends him to the ad, where he finds one."""
    job_id = _setup(con, data_dir)
    con.execute("UPDATE jobs SET contact_strasse='', contact_plz_ort='', "
                "work_strasse='Kundenweg 3', work_plz_ort='10115 Berlin', "
                "temp_agency=1 WHERE id=?", (job_id,))
    con.commit()

    missing = unterlagen.preview(con, job_id)["missing"]
    reasons = {m["key"]: m["why"] for m in missing}
    assert "Arbeitnehmerüberlassung" in reasons["strasse"]
    assert "Arbeitnehmerüberlassung" in reasons["plz_ort"]
    assert "weder Anzeige noch Board" not in reasons["strasse"]


def test_without_ueberlassung_the_ordinary_reason_still_stands(con, data_dir):
    job_id = _setup(con, data_dir)
    con.execute("UPDATE jobs SET contact_strasse='', contact_plz_ort='', "
                "work_strasse='', work_plz_ort='', temp_agency=0 WHERE id=?",
                (job_id,))
    con.commit()

    reasons = {m["key"]: m["why"]
               for m in unterlagen.preview(con, job_id)["missing"]}
    assert "weder Anzeige noch Board" in reasons["strasse"]


# --------------------------------------------------------------------------
# What the spine says about the documents rubric. Untested, the whole rubric
# could report zeros — telling him his Mappe is the letter alone while six
# certificates sit in the folder.
# --------------------------------------------------------------------------
def _template(data_dir) -> pathlib.Path:
    path = data_dir / "vorlage.html"
    path.write_text(TEMPLATE, encoding="utf-8")
    return path


def test_the_rail_counts_the_template_and_every_anlage(con, data_dir):
    folder = data_dir / "anlagen"
    folder.mkdir()
    _blank_pdf(folder / "01_A.pdf")
    _blank_pdf(folder / "02_B.pdf")
    db.set_setting(con, "anlagen_dir", str(folder))
    db.set_setting(con, "template_path", str(_template(data_dir)))
    con.commit()

    facts = unterlagen.rail_facts(con)

    assert facts["documents"] == 3      # the letter template plus two Anlagen
    assert facts["anlagen"] == 2
    assert facts["template_ok"] is True
    assert facts["folder_state"] == "ok"
    assert facts["built"] is False      # nothing has measured it yet


def test_the_rail_does_not_count_a_template_that_is_not_there(con, data_dir):
    folder = data_dir / "anlagen"
    folder.mkdir()
    _blank_pdf(folder / "01_A.pdf")
    db.set_setting(con, "anlagen_dir", str(folder))
    db.set_setting(con, "template_path", str(data_dir / "weg.html"))
    con.commit()

    facts = unterlagen.rail_facts(con)

    assert (facts["template_ok"], facts["documents"]) == (False, 1)


def test_a_specimen_without_a_measurement_does_not_count_as_built(con, data_dir):
    """The letter's own length is what a build writes down. A specimen file
    left behind by an older version measures nothing, and every page span on
    the screen would still be unknown."""
    unterlagen.specimen_path().parent.mkdir(parents=True, exist_ok=True)
    _blank_pdf(unterlagen.specimen_path(), pages=4)
    con.commit()

    assert unterlagen.rail_facts(con)["built"] is False

    db.set_setting(con, unterlagen.LETTER_PAGES_SETTING, "3")
    con.commit()
    assert unterlagen.rail_facts(con)["built"] is True


def test_an_unusable_folder_setting_does_not_take_the_rail_down(con, data_dir):
    """The rail is drawn on EVERY page. `Path.expanduser()` raises on a "~name"
    with no such user, and this value is free text in a field he edits — so one
    typo would have made the whole app unrenderable, settings page included."""
    db.set_setting(con, "anlagen_dir", "~kein-solcher-benutzer/Anlagen")
    db.set_setting(con, "template_path", "~kein-solcher-benutzer/vorlage.html")
    con.commit()

    facts = unterlagen.rail_facts(con)

    assert facts["folder_state"] == "missing"
    assert facts["template_ok"] is False
    assert unterlagen.rail_fingerprint(con) is not None


def test_the_rail_notices_a_certificate_arriving_in_the_folder(con, data_dir):
    """None of this is in a table. Without the folder in the fingerprint the
    spine goes on reading "keine Anlagen" for the life of the page he just
    filled — the staleness class the live watcher exists to end."""
    folder = data_dir / "anlagen"
    folder.mkdir()
    db.set_setting(con, "anlagen_dir", str(folder))
    con.commit()
    before = unterlagen.rail_fingerprint(con)

    _blank_pdf(folder / "01_Zeugnis.pdf")

    assert unterlagen.rail_fingerprint(con) != before


def test_the_rail_notices_the_folder_itself_being_chosen(con, data_dir):
    """A folder just created and still EMPTY fingerprints identically to no
    folder at all if only its contents are signed — so the one press that
    answers "where do I put my documents" left the spine reading "kein Ordner
    für Anlagen" for the life of the page."""
    empty = data_dir / "Anlagen"
    empty.mkdir()
    before = unterlagen.rail_fingerprint(con)          # nothing configured

    db.set_setting(con, "anlagen_dir", str(empty))
    con.commit()

    assert unterlagen.rail_fingerprint(con) != before
    # and moving to a DIFFERENT empty folder is a change too
    other = data_dir / "Anderswo"
    other.mkdir()
    mid = unterlagen.rail_fingerprint(con)
    db.set_setting(con, "anlagen_dir", str(other))
    con.commit()
    assert unterlagen.rail_fingerprint(con) != mid


def test_the_rail_notices_the_template_being_chosen(con, data_dir):
    """Same blind spot on the other path: pointing the setting at a template
    that is not there yet must still reach the rubric."""
    before = unterlagen.rail_fingerprint(con)

    db.set_setting(con, "template_path", str(data_dir / "noch-nicht-da.html"))
    con.commit()

    assert unterlagen.rail_fingerprint(con) != before


def test_the_rail_notices_the_template_being_replaced(con, data_dir):
    path = _template(data_dir)
    db.set_setting(con, "template_path", str(path))
    con.commit()
    before = unterlagen.rail_fingerprint(con)

    path.write_text(TEMPLATE + "<p>eine Seite mehr</p>", encoding="utf-8")

    assert unterlagen.rail_fingerprint(con) != before


def test_a_folder_that_cannot_be_read_is_its_own_state(con, data_dir):
    """Mounted and unreadable answered exactly like empty, and the Mappe built
    from it would be the letter alone — reported as complete."""
    folder = data_dir / "anlagen"
    folder.mkdir()
    _blank_pdf(folder / "01_A.pdf")
    db.set_setting(con, "anlagen_dir", str(folder))
    con.commit()
    folder.chmod(0o000)
    try:
        state = unterlagen.folder_state(str(folder), 0)
    finally:
        folder.chmod(0o755)

    assert state["state"] == "unreadable"
    assert "nicht lesen" in state["note"]
    assert unterlagen.folder_state(str(folder), 1)["state"] == "ok"


# ---------------------------------------------------------------------------
# The ATS check: what a portal's parser makes of the built files
# ---------------------------------------------------------------------------

CV_ATS_HTML = """<html><body><h1>Erika Muster</h1>
<p>erika@example.org · +49 170 1234567</p>
<h2>Profil</h2><p>Fachinformatikerin, ab sofort verfügbar, bundesweit.</p>
<h2>Berufserfahrung</h2><p>Praktikum Softwareentwicklung · 01/2025 – 07/2025
Backend-Entwicklung mit Python und Django, REST-Endpunkte und API-Testing.</p>
<h2>Ausbildung</h2><p>Fachinformatikerin · 08/2022 – 07/2025</p>
<h2>Kenntnisse</h2><p>Python, Django, PostgreSQL, Docker, Git, Linux</p>
</body></html>"""


def _with_cv(con, data_dir):
    cv = data_dir / "cv_ats.html"
    cv.write_text(CV_ATS_HTML, encoding="utf-8")
    db.set_setting(con, "cv_ats_path", str(cv))
    con.commit()
    return cv


def test_nothing_built_means_no_report_rather_than_a_failed_one(con, data_dir):
    _setup(con, data_dir)
    view = unterlagen.read(con, None)
    assert view["ats"] == {"mappe": None, "lebenslauf": None,
                           "cv_configured": False}


async def test_building_renders_the_portal_cv_and_measures_both_files(
    con, data_dir
):
    """The check runs on the PDFs a portal will parse, never on the HTML:
    Type 3 fonts, lost spaces and letter-spaced headings only exist in the
    rendered file."""
    job_id = _setup(con, data_dir)
    _with_cv(con, data_dir)

    result = await unterlagen.build(job_id)

    assert result["ok"], result["error"]
    assert result["cv_error"] == ""
    assert unterlagen.specimen_cv_path().is_file()
    view = unterlagen.read(con, job_id)
    assert view["ats"]["cv_configured"]
    assert view["ats"]["mappe"] is not None
    assert view["ats"]["lebenslauf"] is not None
    cv = view["ats"]["lebenslauf"]
    assert cv.passed, [c.text for c in cv.checks if not c.ok]
    assert cv.pages == 1


async def test_a_missing_portal_cv_is_a_sentence_on_the_build_not_a_crash(
    con, data_dir
):
    job_id = _setup(con, data_dir)
    db.set_setting(con, "cv_ats_path", str(data_dir / "weg.html"))
    con.commit()

    result = await unterlagen.build(job_id)

    assert result["ok"]
    assert result["cv_error"].startswith("Lebenslauf für Portale nicht gefunden")
    assert not unterlagen.specimen_cv_path().exists()
    assert unterlagen.read(con, job_id)["ats"]["lebenslauf"] is None


def test_the_signature_sees_the_portal_cv_template_change(con, data_dir):
    job_id = _setup(con, data_dir)
    cv = _with_cv(con, data_dir)
    before = unterlagen.signature(con, job_id)
    cv.write_text(CV_ATS_HTML + "<!-- edited -->", encoding="utf-8")
    assert unterlagen.signature(con, job_id) != before


async def test_the_signature_sees_the_portal_cv_specimen_being_built(
    con, data_dir
):
    job_id = _setup(con, data_dir)
    _with_cv(con, data_dir)
    before = unterlagen.signature(con, job_id)
    await unterlagen.build(job_id)
    assert unterlagen.signature(con, job_id) != before
