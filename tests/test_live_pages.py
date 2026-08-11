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


async def test_the_dashboard_counts_an_application_recorded_elsewhere(
        user: User, con, data_dir):
    """It was rendered once and never again — the home screen that never moves
    is the strongest single reason the app read as dead."""
    await user.open("/")
    await user.should_see("Applications")

    db.add_bewerbung(con, {"gesendet_am": "2026-08-11", "firma": "Beispiel GmbH",
                           "email": "hr@beispiel.example", "kanal": "E-Mail",
                           "status": "Gesendet"})
    con.commit()
    await _tick(user)

    await user.should_see("Gesendet")


async def test_the_application_registry_shows_a_send_from_another_tab(
        user: User, con, data_dir):
    await user.open("/applications")
    await user.should_see("0 applications")

    db.add_bewerbung(con, {"gesendet_am": "2026-08-11", "firma": "Beispiel GmbH",
                           "email": "hr@beispiel.example", "kanal": "E-Mail",
                           "status": "Gesendet"})
    con.commit()
    await _tick(user)

    await user.should_see("1 applications")


async def test_the_profile_list_shows_a_poll_that_just_happened(
        user: User, con, data_dir):
    profile_id = db.add_profile(con, {"name": "Python", "keywords": "python"})
    con.commit()
    await user.open("/profiles")
    await user.should_see("Python")

    db.mark_profile_polled(con, profile_id, error="jooble: 401 Unauthorized")
    con.commit()
    await _tick(user)

    await user.should_see("401 Unauthorized")


async def test_the_cockpit_fills_its_rows_when_the_draft_finishes(
        user: User, con, data_dir):
    """Its own hint tells him to draft the application first, and the draft is
    written on another screen — so the rows it leaves empty are exactly the
    ones a finished draft fills, a minute later, on a page built to sit open."""
    job_id = _posting(con)
    await user.open(f"/cockpit/{job_id}")
    await user.should_see("Fehlt noch")

    db.upsert_draft(con, job_id, {
        "status": "ready", "betreff": "Bewerbung als Python Entwickler",
        "anschreiben_body": "Sehr geehrte Damen und Herren,"})
    con.commit()
    await _tick(user)

    await user.should_see("Sehr geehrte Damen und Herren,")


async def test_the_cockpit_ignores_a_change_to_another_posting(
        user: User, con, data_dir):
    """It watches ONE posting: rebuilding while he types into an employer's
    form because an unrelated job was scored would move the buttons under his
    hand."""
    job_id = _posting(con)
    await user.open(f"/cockpit/{job_id}")
    await user.should_see("Beispiel GmbH")
    before = [e for e in user.client.elements.values()
              if isinstance(e, ui.button)]

    _posting(con, external_id="e2", title="Django Entwickler",
             company="Zweite GmbH")
    await _tick(user)

    after = [e for e in user.client.elements.values()
             if isinstance(e, ui.button)]
    assert [e.id for e in after] == [e.id for e in before], \
        "the cockpit rebuilt for a posting it is not showing"


async def test_the_settings_meter_follows_the_spend(user: User, con, data_dir):
    """Background scoring spends metered money every ten minutes and the page
    showed the number from whenever it was opened."""
    await user.open("/settings")
    await user.should_see("0 calls")

    db.record_llm_usage(con, input_tokens=1000, output_tokens=200,
                        cost_usd=0.0123)
    con.commit()
    await _tick(user)

    await user.should_see("1 calls")
    await user.should_see("$0.0123")


async def test_the_settings_forms_are_never_overwritten(user: User, con,
                                                        data_dir):
    """The page polls its METERS only: rebuilding a card would throw away
    whatever he is typing into it."""
    db.set_setting(con, "test_recipient", "andrei@example.org")
    con.commit()
    await user.open("/settings")
    field = next(e for e in user.client.elements.values()
                 if isinstance(e, ui.input)
                 and "Test recipient" in (e.props.get("label") or ""))
    field.set_value("halb getippt@example.org")

    db.record_llm_usage(con, input_tokens=10, output_tokens=2, cost_usd=0.01)
    con.commit()
    await _tick(user)

    assert field.value == "halb getippt@example.org"


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


async def test_a_reconnect_does_not_kill_the_self_refresh(user: User, con,
                                                          data_dir):
    """`on_disconnect` fires on every socket drop — a sleeping laptop, a wifi
    blip — and NiceGUI only deletes the client if the browser fails to come
    back. Cancelling the timer there is irreversible, so the page came back,
    rendered fine, and never updated again."""
    _posting(con)
    await user.open("/jobs")
    await user.should_see("Python Entwickler")
    timer = next(e for e in user.client.elements.values()
                 if isinstance(e, ui.timer))

    with user.client:
        for handler in list(user.client.disconnect_handlers):
            handler(user.client)
    assert not timer._is_canceled, "a transient disconnect cancelled the timer"

    for handler in list(user.client.connect_handlers):
        handler(user.client)
    assert timer.active

    _posting(con, external_id="e2", title="Django Entwickler",
             company="Zweite GmbH")
    await _tick(user)
    await user.should_see("Django Entwickler")
