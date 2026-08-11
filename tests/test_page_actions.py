"""What a page shows, which actions it offers — and which it asks about first.

Rendered for real: these are all "which control exists, and what does pressing
it do", which no data-layer test can see.
"""

import asyncio
import sys

import pytest
from nicegui import ui
from nicegui.testing import User

from jobdeck import db

pytest_plugins = ["nicegui.testing.user_plugin"]

pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_main.py")


@pytest.fixture(autouse=True)
def _keep_the_package_importable():
    """See test_draft_visibility_pages.py: NiceGUI's teardown pops the page
    module AND its parents out of sys.modules."""
    saved = {name: mod for name, mod in sys.modules.items()
             if name == "jobdeck" or name.startswith("jobdeck.")}
    yield
    sys.modules.update(saved)


def _posting(con, **over):
    values = {"source": "stub", "external_id": "e1", "title": "Python Entwickler",
              "company": "Beispiel GmbH", "url": "https://beispiel.example/1"}
    values.update(over)
    job_id = db.insert_job_if_new(con, values)
    con.commit()
    return job_id


# ---------------------------------------------------------------------------
# One path to a form application
# ---------------------------------------------------------------------------
async def test_the_inbox_offers_one_way_into_a_form_application(
        user: User, con, data_dir):
    """"Apply via portal" opened the raw posting and marked it 'portal' from
    the row; the cockpit does both and is where the form work happens. Two
    controls for one act is what "too many buttons" meant."""
    _posting(con)
    await user.open("/jobs")
    await user.should_see("Python Entwickler")

    await user.should_see("Formular ausfüllen")
    await user.should_not_see("Apply via portal")


# ---------------------------------------------------------------------------
# A failed draft can be written again where it failed
# ---------------------------------------------------------------------------
async def test_a_failed_draft_can_be_rewritten_from_the_queue(
        user: User, con, data_dir, monkeypatch):
    """It used to send him to the Job inbox, whose Draft button only appears
    while the posting is still 'new' — so a failed draft for a posting he had
    already opened as a form had nowhere at all to be retried."""
    from jobdeck.ui.pages import queue as queue_page

    job_id = _posting(con)
    db.upsert_draft(con, job_id, {"status": "failed", "error": "truncated"})
    db.set_job_status(con, job_id, "portal")
    con.commit()

    called = {"job_id": None}

    async def fake_draft(target_id):
        called["job_id"] = target_id
        with db.db() as own:
            db.upsert_draft(own, target_id, {"status": "ready",
                                             "betreff": "Zweiter Versuch"})
        return {"ok": True, "error": "", "draft": {}}

    monkeypatch.setattr(queue_page.drafting, "draft_for_job", fake_draft)

    await user.open("/queue")
    user.find("Neu schreiben").click()
    await asyncio.sleep(0.3)

    assert called["job_id"] == job_id
    assert db.get_draft_by_job(con, job_id)["status"] == "ready"


# ---------------------------------------------------------------------------
# Deleting real data asks first
# ---------------------------------------------------------------------------
async def test_deleting_a_search_profile_asks_first(user: User, con, data_dir):
    profile_id = db.add_profile(con, {"name": "Python", "keywords": "python"})
    con.commit()
    await user.open("/profiles")

    user.find(marker="delete-profile").click()
    await asyncio.sleep(0.2)
    await user.should_see("löschen?")
    assert db.get_profile(con, profile_id) is not None, "deleted before asking"

    user.find("Abbrechen").click()
    await asyncio.sleep(0.2)
    assert db.get_profile(con, profile_id) is not None

    user.find(marker="delete-profile").click()
    await asyncio.sleep(0.2)
    user.find("Löschen").click()
    await asyncio.sleep(0.3)
    assert db.get_profile(con, profile_id) is None


async def test_deleting_an_application_asks_first(user: User, con, data_dir):
    """A real application, its status history and its link to the sent e-mail
    went on one unconfirmed click."""
    row_id = db.add_bewerbung(con, {"gesendet_am": "2026-08-11",
                                    "firma": "Beispiel GmbH",
                                    "email": "hr@beispiel.example",
                                    "kanal": "E-Mail", "status": "Gesendet"})
    con.commit()
    await user.open("/applications")
    await user.should_see("1 applications")

    # the editor opens on a row click, which is a Quasar event with the row as
    # its second argument — the same payload the page's own handler reads
    row = dict(db.get_bewerbung(con, row_id))
    user.find(kind=ui.table).trigger("rowClick", [None, row])
    await asyncio.sleep(0.2)
    user.find(marker="delete-application").click()
    await asyncio.sleep(0.2)

    await user.should_see("löschen?")
    assert db.get_bewerbung(con, row_id) is not None, "deleted before asking"

    user.find("Abbrechen").click()
    await asyncio.sleep(0.2)
    assert db.get_bewerbung(con, row_id) is not None


async def test_a_confirmed_application_delete_goes_through(user: User, con,
                                                           data_dir):
    row_id = db.add_bewerbung(con, {"gesendet_am": "2026-08-11",
                                    "firma": "Beispiel GmbH",
                                    "email": "hr@beispiel.example",
                                    "kanal": "E-Mail", "status": "Gesendet"})
    con.commit()
    await user.open("/applications")
    await user.should_see("1 applications")
    user.find(kind=ui.table).trigger("rowClick",
                                     [None, dict(db.get_bewerbung(con, row_id))])
    await asyncio.sleep(0.2)
    user.find(marker="delete-application").click()
    await asyncio.sleep(0.2)

    user.find("Löschen").click()
    await asyncio.sleep(0.3)

    assert db.get_bewerbung(con, row_id) is None


# ---------------------------------------------------------------------------
# Facts the board states, on the row
# ---------------------------------------------------------------------------
async def test_a_temp_agency_posting_says_so_on_the_row(user: User, con,
                                                        data_dir):
    """The employer is a staffing firm and the work happens at a client's site
    — worth knowing before writing a letter, and the reason that posting's
    address is never borrowed for one."""
    job_id = _posting(con, company="Zeitarbeit GmbH")
    db.set_job_facts(con, job_id, {"temp_agency": 1, "salary_from": "37000",
                                   "salary_period": "Jahresgehalt"})
    con.commit()

    await user.open("/jobs")

    await user.should_see("Arbeitnehmerüberlassung")
    await user.should_see("Gehalt: 37.000 € (Jahresgehalt)")


# ---------------------------------------------------------------------------
# The send path's last screen
# ---------------------------------------------------------------------------
async def test_send_now_reaches_the_confirmation_with_the_resolved_recipient(
        user: User, con, data_dir):
    """The one screen between him and a real e-mail. It was silently inert on
    this branch — `run.io_bound` called a function that had gained a parameter,
    NiceGUI logged the TypeError, and the button did nothing and said nothing.
    Nothing is sent here: the dialog is where the test stops."""
    job_id = _posting(con, company="Beispiel GmbH")
    db.upsert_draft(con, job_id, {
        "status": "ready", "recipient": "jobs@beispiel.example",
        "betreff": "Bewerbung als Python Entwickler",
        "email_body": "Guten Tag,", "anschreiben_body": "Sehr geehrte Damen,",
        "pdf_path": "/tmp/mappe.pdf"})
    db.set_setting(con, "test_recipient", "probe@example.org")
    con.commit()

    await user.open("/queue")
    user.find("Review & send").click()
    await asyncio.sleep(0.2)
    user.find("Send now").click()
    await asyncio.sleep(0.3)

    await user.should_see("Send this application?")
    await user.should_see("TEST send: probe@example.org")
    assert db.count_outbound_today(con) == 0, "nothing may be sent by a test"


# ---------------------------------------------------------------------------
# Settings that reach the queries they configure
# ---------------------------------------------------------------------------
async def test_the_age_threshold_he_types_is_the_one_that_gets_saved(
        user: User, con, data_dir):
    """Every test of the threshold so far wrote the setting directly, so the
    input could have been disconnected from it with the suite green."""
    await user.open("/settings")
    field = next(e for e in user.client.elements.values()
                 if isinstance(e, ui.number)
                 and "alt nach" in (e.props.get("label") or ""))
    field.set_value(21)
    await asyncio.sleep(0.1)

    user.find(marker="save-tunables").click()
    await asyncio.sleep(0.3)

    assert db.get_setting(con, "stale_age_days", "") == "21"


# ---------------------------------------------------------------------------
# The warning that stands between him and a refused send
# ---------------------------------------------------------------------------
async def test_the_queue_shows_the_duplicate_warning_on_the_row(
        user: User, con, data_dir):
    job_id = _posting(con, company="Beispiel GmbH")
    db.upsert_draft(con, job_id, {
        "status": "ready", "recipient": "jobs@beispiel.example",
        "betreff": "Bewerbung als Python Entwickler",
        "email_body": "Guten Tag,", "anschreiben_body": "Sehr geehrte Damen,",
        "pdf_path": "/tmp/mappe.pdf"})
    db.add_bewerbung(con, {"gesendet_am": "2026-06-12", "firma": "Beispiel GmbH",
                           "email": "", "kanal": "E-Mail", "status": "Absage"})
    con.commit()

    await user.open("/queue")

    await user.should_see("bereits beworben")
    await user.should_see("Absage")


async def test_the_send_confirmation_repeats_the_duplicate_warning(
        user: User, con, data_dir):
    """The last statement before the press — and the send path refuses it a
    second later anyway."""
    job_id = _posting(con, company="Beispiel GmbH")
    db.upsert_draft(con, job_id, {
        "status": "ready", "recipient": "jobs@beispiel.example",
        "betreff": "Bewerbung als Python Entwickler",
        "email_body": "Guten Tag,", "anschreiben_body": "Sehr geehrte Damen,",
        "pdf_path": "/tmp/mappe.pdf"})
    db.add_bewerbung(con, {"gesendet_am": "2026-06-12", "firma": "Beispiel GmbH",
                           "email": "", "kanal": "E-Mail", "status": "Absage"})
    db.set_setting(con, "test_recipient", "probe@example.org")
    con.commit()
    await user.open("/queue")
    user.find("Review & send").click()
    await asyncio.sleep(0.2)

    user.find("Send now").click()
    await asyncio.sleep(0.3)

    # scoped to the dialog: the row behind it carries the same sentence, so a
    # page-wide search would pass with the dialog's own line deleted
    confirm = next(e for e in user.client.elements.values()
                   if isinstance(e, ui.dialog) and e.value
                   and any("Send this application?" in getattr(d, "text", "")
                           for d in e.descendants()))
    shown = [getattr(el, "text", "") for el in confirm.descendants()]
    assert any("Send this application?" in (t or "") for t in shown)
    assert any("bereits beworben" in (t or "") for t in shown), \
        "the last screen before the press does not repeat the warning"
