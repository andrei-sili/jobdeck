"""Pages that keep up with the engine, rendered for real.

The mechanism's decision table is unit-tested in test_live.py; what these prove
is the wiring — that a page really rebuilds itself when the background writes
something, and really holds off while he is reading a posting. Both are
invisible to any data-layer test: the whole defect class was "the query is
right, nothing asks it again".
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


def _posting(con, external_id="e1", title="Python Entwickler",
             company="Beispiel GmbH"):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": external_id, "title": title,
        "company": company, "url": "https://beispiel.example/1",
    })
    con.commit()
    return job_id


async def _tick(user: User) -> None:
    """Fire the page's live timer by hand — the interval is half a minute.

    Inside the client context, which is what NiceGUI's own timer loop enters
    before every invocation (`Timer._get_context`); the tick reads the page's
    elements to decide whether he is busy."""
    timer = next(e for e in user.client.elements.values()
                 if isinstance(e, ui.timer))
    with user.client:
        await timer.callback()
    await asyncio.sleep(0.1)


def _expansions(user: User):
    return [e for e in user.client.elements.values()
            if isinstance(e, ui.expansion)]


async def test_a_posting_stored_by_the_poller_appears_by_itself(
        user: User, con, data_dir):
    """The literal complaint: profiles poll hourly and 50-100 postings a day
    arrived into a list that only a click would ever re-read."""
    _posting(con)
    await user.open("/jobs")
    await user.should_see("Python Entwickler")

    _posting(con, external_id="e2", title="Django Entwickler",
             company="Zweite GmbH")
    await _tick(user)

    await user.should_see("Django Entwickler")


async def test_nothing_new_rebuilds_nothing(user: User, con, data_dir):
    """The gate is the signature, not the clock: an unchanged database must
    leave the page exactly as it is, expansions and all."""
    _posting(con)
    await user.open("/jobs")
    await user.should_see("Python Entwickler")
    expansion = _expansions(user)[0]
    expansion.set_value(True)
    await asyncio.sleep(0.1)

    await _tick(user)

    assert _expansions(user)[0] is expansion, "the page was rebuilt for nothing"
    assert expansion.value is True


async def test_a_posting_he_is_reading_is_not_pulled_out_from_under_him(
        user: User, con, data_dir):
    """A rebuild collapses every open expansion. While one is open the fresh
    data waits behind the chip — and lands by itself once he closes the row."""
    _posting(con)
    await user.open("/jobs")
    await user.should_see("Python Entwickler")
    expansion = _expansions(user)[0]
    expansion.set_value(True)
    await asyncio.sleep(0.1)

    _posting(con, external_id="e2", title="Django Entwickler",
             company="Zweite GmbH")
    await _tick(user)

    await user.should_not_see("Django Entwickler")
    await user.should_see("Neue Daten")
    assert expansion.value is True, "his open posting was collapsed"

    expansion.set_value(False)
    await asyncio.sleep(0.1)
    await _tick(user)

    await user.should_see("Django Entwickler")
    await user.should_not_see("Neue Daten")


async def test_the_chip_hands_over_the_waiting_data_when_pressed(
        user: User, con, data_dir):
    _posting(con)
    await user.open("/jobs")
    await user.should_see("Python Entwickler")
    _expansions(user)[0].set_value(True)
    await asyncio.sleep(0.1)
    _posting(con, external_id="e2", title="Django Entwickler",
             company="Zweite GmbH")
    await _tick(user)
    await user.should_see("Neue Daten")

    user.find("Neue Daten").click()
    await asyncio.sleep(0.2)

    await user.should_see("Django Entwickler")


async def test_an_open_dialog_also_defers_the_rebuild(
        user: User, con, data_dir):
    """A refresh deletes the list a dialog was opened over; the editor and the
    two `await confirm` flows would go with it."""
    job_id = _posting(con)
    db.upsert_draft(con, job_id, {
        "status": "ready", "betreff": "Bewerbung als Python Entwickler",
        "recipient": "jobs@beispiel.example"})
    con.commit()
    await user.open("/jobs")
    user.find("Draft application").click()
    await asyncio.sleep(0.2)
    await user.should_see("Draft — Python Entwickler")

    _posting(con, external_id="e2", title="Django Entwickler",
             company="Zweite GmbH")
    await _tick(user)

    await user.should_not_see("Django Entwickler")
    await user.should_see("Draft — Python Entwickler")
