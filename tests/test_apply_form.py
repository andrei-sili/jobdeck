"""What a German application form asks for, and what JobDeck answers.

Nothing here touches the employer's page — portals are never automated.
These tests are about the ANSWERS being exactly right, because a Referenznummer
or a Stellenbezeichnung that is nearly right is worse than a gap he can see.
"""

import ast
import pathlib

import pytest

from jobdeck import apply_form
from jobdeck.ui.pages import jobs as jobs_page
from jobdeck.ui.pages import settings

_JOB = {
    "id": 7,
    "title": "Ab sofort: Fullstack-Entwickler Python/Django (m/w/d)Vollzeit",
    "company": "Sigtronic GmbH",
    "refnr": "10001-1003387672-S",
    "source": "arbeitsagentur",
    "ansprechpartner": "Frau Georg",
}
_DRAFT = {
    "status": "ready",
    "betreff": "Bewerbung als Fullstack-Entwickler Python/Django (m/w/d), "
               "10001-1003387672-S – Andrei Sili",
    "anschreiben_body": "Sehr geehrte Frau Georg,\n\n" + "Absatz. " * 40,
    "pdf_path": "/home/x/.local/share/jobdeck/output/job_7/Bewerbung.pdf",
}
_SETTINGS = {
    "applicant_name": "Andrei Sili",
    "applicant_email": "bewerbung@example.org",
    "applicant_phone": "+49 000 0000000",
    "applicant_strasse": "Musterstraße 1",
    "applicant_plz_ort": "12345 Musterstadt",
    "applicant_linkedin": "linkedin.com/in/mustermann",
    "applicant_github": "github.com/mustermann",
    "applicant_website": "mustermann.example.org",
    "applicant_availability": "ab sofort",
    "applicant_salary": "45.000 EUR",
}


def _by_label(rows):
    return {row.label: row for row in rows}


def test_the_settings_card_and_the_form_answers_share_one_list_of_fields():
    """Adding a field must be one edit. The labels the Settings card renders are
    the labels the form rows use."""
    assert apply_form.APPLICANT_SETTINGS == tuple(apply_form.APPLICANT_LABELS)
    rows = _by_label(apply_form.personal_fields(_SETTINGS))
    for key, label in apply_form.APPLICANT_LABELS.items():
        if key == "applicant_name":
            # the one setting a form asks for in two boxes
            assert "Vorname" in rows and "Nachname" in rows
            continue
        assert label in rows, f"{key} has no form row"


def test_every_applicant_setting_reaches_the_form_answers():
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
                            {"status": "ready", "betreff": 7},
                            {"applicant_name": None})
    assert _by_label(odd)["Referenznummer"].value == "12345"
    assert _by_label(odd)["Betreff"].value == "7"


def test_the_settings_page_offers_every_field_the_form_needs():
    """A field the form asks for but Settings cannot fill would be a
    permanent gap."""
    source = pathlib.Path(settings.__file__).read_text()
    assert "apply_form.APPLICANT_LABELS" in source      # rendered from the list
    assert "apply_form.APPLICANT_SETTINGS" in source    # and loaded from it
    # the name stays owned by exactly ONE input, or two Save buttons fight over
    # the same setting and the last card saved silently wins
    assert source.count("applicant_name = ui.input(") == 1
    assert 'if key == "applicant_name":' in source, (
        "the Bewerbungsdaten card must skip the name the Application card owns")


@pytest.mark.parametrize("status, offered", [
    ("ready", True),
    ("approved", True),
    ("sent", True),
    ("discarded", False),   # he threw those words away
    ("failed", False),      # they were never finished
    ("generating", False),
    ("", False),
])
def test_only_a_usable_draft_answers_the_form(status, offered):
    """Job 18's draft is discarded because the ad is gone. Offering its text as
    the Anschreiben would put words he rejected into someone's form."""
    rows = _by_label(apply_form.fields(_JOB, {**_DRAFT, "status": status}, {}))
    assert rows["Anschreiben"].ready is offered
    assert rows["Betreff"].ready is offered
    assert rows["Bewerbungsmappe (PDF)"].ready is offered
    if not offered:
        assert rows["Anschreiben"].hint  # and it says what to do instead


@pytest.mark.parametrize("job, expected, why", [
    ({"source": "arbeitsagentur", "external_id": "10001-1003292975-S", "refnr": ""},
     "10001-1003292975-S",
     "186 of his 209 Arbeitsagentur postings have an EMPTY refnr column, and the "
     "external_id IS the Referenznummer — reading the column raw would say "
     "'none stated' while the Betreff row two lines down prints it"),
    ({"source": "arbeitsagentur", "external_id": "10001-999-S", "refnr": "REF-42"},
     "REF-42", "an extracted value wins over the id"),
    ({"source": "jooble", "external_id": "987654", "refnr": ""},
     "", "a Jooble id is NOT a Referenznummer and must not be offered as one"),
    ({"source": "arbeitnow", "external_id": "acme-dev-1", "refnr": ""},
     "", "nor an Arbeitnow slug"),
])
def test_the_referenznummer_row_uses_the_apps_own_resolver(job, expected, why):
    rows = _by_label(apply_form.posting_fields({**_JOB, **job}, None))
    assert rows["Referenznummer"].value == expected, why
    if not expected:
        assert rows["Referenznummer"].hint  # says so rather than inventing one


def test_the_screen_that_opens_a_form_never_reaches_the_employers_page():
    """Portals are never automated. The app may open a form in HIS browser and
    nothing else — no fetch, no submit, no client.

    This rule used to live over the cockpit; the cockpit is gone and the
    posting screen took its job, so the rule moved with it rather than being
    deleted along with the file it happened to be written against."""
    source = pathlib.Path(jobs_page.__file__).read_text()
    tree = ast.parse(source)
    banned = {"httpx", "requests", "urlopen", "AsyncClient", "probe_status"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned:
            raise AssertionError(
                f"jobs.py:{node.lineno} reaches the network: {node.attr}")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {a.name for a in node.names}
            names.add(getattr(node, "module", None) or "")
            assert not (names & {"httpx", "requests"}), (
                f"jobs.py:{node.lineno} imports a network client")


def test_the_cockpits_old_address_still_lands_somewhere_real():
    """The page is gone, not the link. An old tab or a bookmark answers as
    moved, the way /jobs already does — never a 404 and never a blank page
    that then navigates."""
    source = pathlib.Path(jobs_page.__file__).read_text()
    assert '@app.get("/cockpit/{job_id}")' in source
    assert "def legacy_cockpit_page(" in source
    # and nothing in the UI still tries to send him there
    for path in pathlib.Path(jobs_page.__file__).parent.glob("*.py"):
        text = path.read_text()
        assert 'navigate.to(f"/cockpit' not in text, f"{path.name} still links in"


def test_the_form_answers_never_offer_a_letter_he_threw_away():
    """`posting_fields` does NOT apply `usable()` — only `fields()` does — so
    a caller that reaches for it directly would hand a discarded or failed
    draft to an employer's form. The strip's Formulardaten sheet is such a
    caller, and it applies `usable()` itself."""
    source = pathlib.Path(jobs_page.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "posting_fields"):
            second = node.args[1] if len(node.args) > 1 else None
            assert (isinstance(second, ast.Call)
                    and isinstance(second.func, ast.Attribute)
                    and second.func.attr == "usable"), (
                f"jobs.py:{node.lineno} offers a draft that was never screened "
                f"by apply_form.usable()")


def test_a_shortened_label_never_shortens_what_is_copied():
    """A truncated Referenznummer in someone's form is worse than none at all:
    an id is either exact or wrong."""
    long_value = "10001-1003387672-S-und-noch-viel-mehr-text-" + "x" * 80
    assert jobs_page._short(long_value).endswith("…")
    assert len(jobs_page._short(long_value)) <= 60
    # the value itself is untouched — only the label is shortened
    source = pathlib.Path(jobs_page.__file__).read_text()
    assert "ui.clipboard.write(v)" in source
    assert "ui.clipboard.write(_short" not in source
