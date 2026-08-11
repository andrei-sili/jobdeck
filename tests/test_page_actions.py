"""Actions a page offers — and the ones it must ask about first.

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
