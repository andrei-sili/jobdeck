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
                            {"status": "ready", "betreff": 7},
                            {"applicant_name": None})
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


# ---------------------------------------------------------------------------
# The cockpit page's own helpers. Everything above tests the pure module or the
# source text; these EXECUTE the functions the page calls, which is what the
# review found missing — the status guard and the recorded Kanal could both be
# broken with the suite green.
# ---------------------------------------------------------------------------
def _seed_job(con, **over):
    from jobdeck import db
    values = {"source": "arbeitsagentur", "external_id": "10001-999-S",
              "title": "Python Entwickler (m/w/d)", "company": "Formular GmbH",
              "url": "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-999-S"}
    values.update(over)
    return db.insert_job_if_new(con, values)


def test_load_reads_the_draft_of_THIS_posting(con, data_dir):
    """A draft looked up by the wrong id would offer another company's
    Anschreiben into this employer's form."""
    from jobdeck import db
    mine = _seed_job(con)
    other = _seed_job(con, external_id="other", company="Andere AG")
    db.upsert_draft(con, other, {"status": "ready", "betreff": "FALSCH",
                                 "anschreiben_body": "Sehr geehrte Andere AG,",
                                 "email_body": "x", "pdf_path": "/tmp/other.pdf"})
    db.upsert_draft(con, mine, {"status": "ready", "betreff": "RICHTIG",
                                "anschreiben_body": "Sehr geehrte Formular GmbH,",
                                "email_body": "x", "pdf_path": "/tmp/mine.pdf"})
    con.commit()

    view = cockpit._load(mine)
    assert view["job"]["id"] == mine
    assert view["draft"]["betreff"] == "RICHTIG"
    rows = _by_label(apply_form.fields(view["job"], view["draft"], view["settings"]))
    assert "Formular GmbH" in rows["Anschreiben"].value
    assert cockpit._load(999999) is None          # a vanished posting, not a crash


def test_load_collects_exactly_the_applicant_settings(con, data_dir):
    from jobdeck import db
    job_id = _seed_job(con)
    db.set_setting(con, "applicant_phone", "+49 000 0000000")
    db.set_setting(con, "unrelated_setting", "must not appear")
    con.commit()
    view = cockpit._load(job_id)
    assert set(view["settings"]) == set(apply_form.APPLICANT_SETTINGS)
    assert view["settings"]["applicant_phone"] == "+49 000 0000000"


@pytest.mark.parametrize("start, expected", [
    ("new", "portal"),        # opening the form is the start of applying
    ("portal", "portal"),     # already there, unchanged
    ("skipped", "skipped"),   # a posting he ruled out is not dragged back
    ("applied", "applied"),   # nor one already sent
])
def test_opening_the_form_moves_only_a_new_posting(con, data_dir, start, expected):
    from jobdeck import db
    job_id = _seed_job(con)
    con.execute("UPDATE jobs SET status=? WHERE id=?", (start, job_id))
    con.commit()
    cockpit._mark_portal(job_id)
    assert db.get_job(con, job_id)["status"] == expected


def test_marking_a_vanished_posting_does_not_raise(con, data_dir):
    # the row can be gone by the time the click lands
    cockpit._mark_portal(999999)


def test_recording_goes_through_the_one_application_per_company_gate(con, data_dir):
    from jobdeck import db
    job_id = _seed_job(con)
    con.execute("UPDATE jobs SET status='portal' WHERE id=?", (job_id,))
    con.commit()

    bewerbung_id = cockpit._record(job_id, "Online-Portal")
    assert bewerbung_id is not None
    app = db.get_bewerbung(con, bewerbung_id)
    assert app["firma"] == "Formular GmbH"
    assert app["kanal"] == "Online-Portal"        # a form application, not e-mail
    assert db.get_job(con, job_id)["status"] == "applied"

    # a second posting at the same company is refused by the gate, not recorded
    twin = _seed_job(con, external_id="twin")
    con.commit()
    assert cockpit._record(twin, "Online-Portal") is None
    assert db.get_job(con, twin)["status"] == "duplicate"


def test_the_record_button_is_offered_only_where_recording_finishes_something():
    # its second press on an applied posting would make the posting a
    # 'duplicate' of its own application
    assert cockpit.RECORDABLE_STATUS == ("new", "portal")
    source = pathlib.Path(cockpit.__file__).read_text()
    assert 'if job["status"] in RECORDABLE_STATUS:' in source


@pytest.mark.parametrize("channel, vendor, expected", [
    ("direct_email", "", "Direkt per E-Mail"),
    ("ats_form", "JOIN", "Formular bei JOIN"),
    ("board_apply", "Arbeitnow", "Formular bei Arbeitnow"),
    ("ats_form", "", "Formular in einem Portal"),
    ("company_site", "", "Formular auf der Firmen-Website"),
    ("", "", "Kanal noch nicht ermittelt"),
])
def test_the_channel_line_names_where_the_application_goes(channel, vendor,
                                                          expected):
    line = cockpit._channel_line({"apply_channel": channel, "ats_vendor": vendor})
    assert line.startswith(expected)


def test_the_cockpit_never_reaches_the_employers_page_itself():
    """Portals are never automated. The page may open the form in HIS browser
    and nothing else — no fetch, no submit, no client."""
    source = pathlib.Path(cockpit.__file__).read_text()
    for forbidden in ("httpx", "netsafe.fetch", "probe_status", "requests",
                      "urlopen", "AsyncClient"):
        assert forbidden not in source, f"the cockpit reaches the network: {forbidden}"


def test_the_cockpit_route_is_registered_by_the_app():
    """Dropping the import would 404 the whole feature with the suite green."""
    import inspect

    from jobdeck.ui import app as ui_app
    assert "cockpit" in inspect.getsource(ui_app)
    assert any('"/cockpit/{job_id}"' in line
               for line in pathlib.Path(cockpit.__file__).read_text().splitlines())


@pytest.mark.parametrize("channel", ["ats_form", "board_apply", "company_site"])
def test_the_main_button_leads_into_the_cockpit_wherever_a_form_is_filled(channel):
    """The cockpit is the ONE form path: it opens the employer's page itself
    and keeps every field a click away beside it. Two controls for one act is
    what "too many buttons" meant."""
    from jobdeck.ui.pages import jobs as jobs_page
    steps = {s.key: s for s in jobs_page.apply_steps(
        {"status": "new", "apply_channel": channel, "contact_email": "",
         "draft_status": None, "draft_updated_at": None, "pdf_path": "",
         "url": "https://firma.de/stelle", "apply_url": "",
         "company": "Eine GmbH"})}
    assert jobs_page.STEP_FORM in steps
    assert steps[jobs_page.STEP_FORM].enabled

    source = pathlib.Path(jobs_page.__file__).read_text()
    assert 'ui.navigate.to(url, new_tab=True)' in source, (
        "the form step must open the employer's page through the shared gate")


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
