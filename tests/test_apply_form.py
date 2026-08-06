"""What a German application form asks for, and what the cockpit answers.

The cockpit never touches the employer's page — portals are never automated.
These tests are about the ANSWERS being exactly right, because a Referenznummer
or a Stellenbezeichnung that is nearly right is worse than a gap he can see.
"""

import ast
import inspect
import pathlib

import pytest

from jobdeck import apply_form
from jobdeck.ui.pages import cockpit, settings

_JOB = {
    "id": 7,
    "title": "Ab sofort: Fullstack-Entwickler Python/Django (m/w/d)Vollzeit",
    "company": "Sigtronic GmbH",
    "refnr": "10001-1003387672-S",
    "source": "arbeitsagentur",
    "ansprechpartner": "Frau Georg",
}
_DRAFT = {
    "betreff": "Bewerbung als Fullstack-Entwickler Python/Django (m/w/d), "
               "10001-1003387672-S – Andrei Sili",
    "anschreiben_body": "Sehr geehrte Frau Georg,\n\n" + "Absatz. " * 40,
    "pdf_path": "/home/x/.local/share/jobdeck/output/job_7/Bewerbung.pdf",
}
_SETTINGS = {
    "applicant_name": "Andrei Sili",
    "applicant_email": "a@example.de",
    "applicant_phone": "+49 151 000",
    "applicant_strasse": "Musterweg 1",
    "applicant_plz_ort": "52062 Aachen",
    "applicant_linkedin": "linkedin.com/in/muster",
    "applicant_github": "github.com/muster",
    "applicant_website": "muster.dev",
    "applicant_availability": "ab sofort",
    "applicant_salary": "45.000 EUR",
}


def _by_label(rows):
    return {row.label: row for row in rows}


def test_the_settings_card_and_the_cockpit_share_one_list_of_fields():
    """Adding a field must be one edit. The labels the Settings card renders are
    the labels the cockpit rows use."""
    assert apply_form.APPLICANT_SETTINGS == tuple(apply_form.APPLICANT_LABELS)
    rows = _by_label(apply_form.personal_fields(_SETTINGS))
    for key, label in apply_form.APPLICANT_LABELS.items():
        if key == "applicant_name":
            # the one setting a form asks for in two boxes
            assert "Vorname" in rows and "Nachname" in rows
            continue
        assert label in rows, f"{key} has no cockpit row"


def test_every_applicant_setting_reaches_the_cockpit():
    rows = apply_form.personal_fields(_SETTINGS)
    values = {row.value for row in rows}
    for key, value in _SETTINGS.items():
        if key == "applicant_name":
            continue
        assert value in values, f"{key} is collected but never shown"
    assert apply_form.missing(rows) == []


@pytest.mark.parametrize("full, first, last", [
    ("Andrei Sili", "Andrei", "Sili"),
    ("Anna Maria Müller", "Anna Maria", "Müller"),
    ("  Andrei   Sili  ", "Andrei", "Sili"),
    ("Prince", "Prince", ""),
    ("", "", ""),
])
def test_the_name_splits_the_way_a_german_form_asks_for_it(full, first, last):
    rows = _by_label(apply_form.personal_fields({"applicant_name": full}))
    assert rows["Vorname"].value == first
    assert rows["Nachname"].value == last


def test_the_posting_answers_come_from_the_posting_not_from_prose():
    rows = _by_label(apply_form.posting_fields(_JOB, _DRAFT))
    # board noise stripped, the role name and its (m/w/d) marker intact
    assert rows["Stellenbezeichnung"].value == \
        "Fullstack-Entwickler Python/Django (m/w/d)"
    # an id is either exact or wrong: passed through untouched
    assert rows["Referenznummer"].value == "10001-1003387672-S"
    assert rows["Ansprechpartner"].value == "Frau Georg"
    assert rows["Gefunden über"].value.startswith("Bundesagentur für Arbeit")
    assert rows["Anschreiben"].value == _DRAFT["anschreiben_body"].strip()
    assert rows["Anschreiben"].multiline is True
    assert rows["Bewerbungsmappe (PDF)"].value == _DRAFT["pdf_path"]


def test_an_unknown_source_answers_with_its_own_name_rather_than_nothing():
    rows = _by_label(apply_form.posting_fields({**_JOB, "source": "neuesboard"},
                                               _DRAFT))
    assert rows["Gefunden über"].value == "neuesboard"


def test_a_gap_says_where_to_fill_it_instead_of_rendering_empty():
    """A blank in a Bewerbung is worse than a gap he can see."""
    rows = apply_form.fields(_JOB, None, {})
    gaps = apply_form.missing(rows)
    labels = [row.label for row in gaps]
    assert "Betreff" in labels and "Anschreiben" in labels  # no draft yet
    assert "Telefon" in labels                              # no setting yet
    assert "Stellenbezeichnung" not in labels               # the posting has one
    for row in gaps:
        assert row.hint, f"{row.label} is empty and says nothing"
        assert not row.ready


def test_nothing_raises_on_an_empty_posting_or_odd_types():
    rows = apply_form.fields({}, None, {})
    assert [row.label for row in rows]                # the shape still renders
    assert all(not row.ready for row in rows)
    # a wrong-typed field from a feed must not blow up a page
    odd = apply_form.fields({"title": None, "refnr": 12345, "source": None},
                            {"betreff": 7}, {"applicant_name": None})
    assert _by_label(odd)["Referenznummer"].value == "12345"
    assert _by_label(odd)["Betreff"].value == "7"


def test_the_cockpit_records_the_application_through_the_duplicate_gate(con,
                                                                       data_dir):
    """One application per company is enforced in db.apply_job; the cockpit must
    go through it rather than writing a record of its own."""
    source = inspect.getsource(cockpit)
    assert "db.apply_job(" in source
    tree = ast.parse(source)
    inserts = [n for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and "INSERT INTO" in n.value.upper()]
    assert inserts == [], "the cockpit writes its own SQL instead of apply_job"


def test_the_cockpit_never_navigates_to_an_unscreened_url():
    """`ui.navigate.to` is window.open in the app's own origin, and the form URL
    comes from a board feed."""
    source = pathlib.Path(cockpit.__file__).read_text()
    assert "openable_url(" in source
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "to" and node.args):
            assert not isinstance(node.args[0], ast.Subscript), (
                f"cockpit.py:{node.lineno} navigates straight to a stored field")


def test_the_settings_page_offers_every_field_the_cockpit_needs():
    """A field the cockpit asks for but Settings cannot fill would be a
    permanent gap."""
    source = pathlib.Path(settings.__file__).read_text()
    assert "apply_form.APPLICANT_LABELS" in source      # rendered from the list
    assert "apply_form.APPLICANT_SETTINGS" in source    # and loaded from it
    # the name stays owned by exactly ONE input, or two Save buttons fight over
    # the same setting and the last card saved silently wins
    assert source.count("applicant_name = ui.input(") == 1
    assert 'if key == "applicant_name":' in source, (
        "the Bewerbungsdaten card must skip the name the Application card owns")
