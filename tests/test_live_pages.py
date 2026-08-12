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
    """Fire every live timer on the page by hand — the interval is half a minute.

    EVERY one, not the first: since the redesign a screen carries two watchers,
    its own and the rail's, and picking one by position would silently start
    testing the wrong thing the moment the render order changed.

    Inside the client context, which is what NiceGUI's own timer loop enters
    before every invocation (`Timer._get_context`); the tick reads the page's
    elements to decide whether he is busy."""
    timers = [e for e in list(user.client.elements.values())
              if isinstance(e, ui.timer)]
    assert timers, "the page has no live timer at all"
    with user.client:
        for timer in timers:
            await timer.callback()
    await asyncio.sleep(0.1)


def _emailable(con, **over):
    """A posting the screen offers to write an e-mail application for."""
    job_id = _posting(con, **over)
    db.set_apply_channel(con, job_id, "direct_email", "", "")
    con.commit()
    return job_id


async def _press(user: User, key: str) -> None:
    """A key, through NiceGUI's own inbound event path.

    Not the page's handler called directly: the framework is what decides a
    keystroke inside an input never reaches the page at all, and that rule is
    exactly what a test of the keyboard has to exercise."""
    from nicegui.events import GenericEventArguments
    keyboard = next(e for e in user.client.elements.values()
                    if isinstance(e, ui.keyboard))
    with user.client:
        keyboard._handle_key(GenericEventArguments(
            sender=keyboard, client=user.client, args={
                "action": "keydown", "repeat": False, "key": key,
                "code": f"Key{key.upper()}", "location": 0,
                "altKey": False, "ctrlKey": False, "metaKey": False,
                "shiftKey": False}))
    await asyncio.sleep(0.4)


def _rows(user: User):
    """The list's own rows, in the order they are drawn."""
    return [e for e in user.client.elements.values()
            if "jd-row" in getattr(e, "_classes", [])]


def _selected(user: User):
    return [e for e in _rows(user) if e.props.get("aria-selected") == "true"]


async def test_a_posting_stored_by_the_poller_appears_by_itself(
        user: User, con, data_dir):
    """The literal complaint: profiles poll hourly and 50-100 postings a day
    arrived into a list that only a click would ever re-read."""
    _posting(con)
    await user.open("/")
    await user.should_see("Python Entwickler")

    _posting(con, external_id="e2", title="Django Entwickler",
             company="Zweite GmbH")
    await _tick(user)

    await user.should_see("Django Entwickler")


async def test_nothing_new_rebuilds_nothing(user: User, con, data_dir):
    """The gate is the signature, not the clock: an unchanged database must
    leave the page exactly as it is, down to the very row elements."""
    _posting(con)
    await user.open("/")
    await user.should_see("Python Entwickler")
    row = _rows(user)[0]

    await _tick(user)

    assert _rows(user)[0] is row, "the page was rebuilt for nothing"


async def test_the_posting_he_is_reading_survives_a_new_arrival(
        user: User, con, data_dir):
    """The old inbox had to HOLD BACK fresh data, because a rebuild collapsed
    the expansion he was reading. A reading pane is not part of the list, so
    the arrival simply lands and he keeps his place — the deferral that used to
    be necessary is now the thing that would be in the way."""
    first = _posting(con)
    await user.open("/")
    await user.should_see("Python Entwickler")
    assert [e.props.get("aria-selected") for e in _rows(user)] == ["true"]

    _posting(con, external_id="e2", title="Django Entwickler",
             company="Zweite GmbH")
    await _tick(user)

    await user.should_see("Django Entwickler")   # it landed by itself
    await user.should_see("Python Entwickler")   # and he is still on his row
    assert len(_selected(user)) == 1
    assert db.get_job(con, first)["title"] == "Python Entwickler"


async def test_the_dashboard_counts_an_application_recorded_elsewhere(
        user: User, con, data_dir):
    """It was rendered once and never again — a screen that never moves is the
    strongest single reason the app read as dead."""
    await user.open("/dashboard")
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


async def test_an_open_dialog_defers_the_rebuild(user: User, con, data_dir,
                                                 monkeypatch):
    """A refresh deletes the list a dialog was opened over; the editor and the
    two `await confirm` flows would go with it."""
    from jobdeck.ui.pages import jobs as jobs_page

    async def finished_draft(job_id):
        return {"ok": True, "error": "", "draft": {
            "recipient": "jobs@beispiel.example", "betreff": "Bewerbung",
            "email_body": "…", "anschreiben_body": "…", "llm_model": "stub",
            "pdf_path": ""}}

    monkeypatch.setattr(jobs_page.drafting, "draft_for_job", finished_draft)
    _emailable(con)
    await user.open("/")
    user.find("Bewerbung per E-Mail erstellen").click()
    await asyncio.sleep(0.3)
    await user.should_see("Entwurf — Python Entwickler")

    _posting(con, external_id="e2", title="Django Entwickler",
             company="Zweite GmbH")
    await _tick(user)

    await user.should_not_see("Django Entwickler")
    await user.should_see("Entwurf — Python Entwickler")


async def test_a_reconnect_does_not_kill_the_self_refresh(user: User, con,
                                                          data_dir):
    """`on_disconnect` fires on every socket drop — a sleeping laptop, a wifi
    blip — and NiceGUI only deletes the client if the browser fails to come
    back. Cancelling the timer there is irreversible, so the page came back,
    rendered fine, and never updated again."""
    _posting(con)
    await user.open("/")
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


async def test_a_closed_dialog_does_not_freeze_the_page(user: User, con,
                                                        data_dir, monkeypatch):
    """Pages keep exactly one dialog element alive after closing it, so
    "does a dialog EXIST" would be true forever after the first one — and the
    page would defer every update for the rest of its life."""
    from jobdeck.ui.pages import jobs as jobs_page

    async def finished_draft(job_id):
        return {"ok": True, "error": "", "draft": {
            "recipient": "jobs@beispiel.example", "betreff": "Bewerbung",
            "email_body": "…", "anschreiben_body": "…", "llm_model": "stub",
            "pdf_path": ""}}

    monkeypatch.setattr(jobs_page.drafting, "draft_for_job", finished_draft)
    _emailable(con)
    await user.open("/")
    user.find("Bewerbung per E-Mail erstellen").click()
    await asyncio.sleep(0.3)
    await user.should_see("Entwurf — Python Entwickler")
    user.find("Schließen").click()
    await asyncio.sleep(0.2)
    assert any(isinstance(e, ui.dialog) for e in user.client.elements.values()), \
        "the dialog element is gone, so this proves nothing"

    _posting(con, external_id="e2", title="Django Entwickler",
             company="Zweite GmbH")
    await _tick(user)

    await user.should_see("Django Entwickler")


async def test_a_row_action_leaves_the_page_able_to_update_itself(
        user: User, con, data_dir):
    """Every row action ends in a refresh that deletes the element the action
    was fired from. If that left the page believing he is still busy it would
    defer every future update for the rest of the session."""
    _posting(con)
    await user.open("/")
    await user.should_see("Python Entwickler")

    user.find("✕ kein Interesse").click()
    await asyncio.sleep(0.3)
    await user.should_not_see("Python Entwickler")

    _posting(con, external_id="e2", title="Django Entwickler",
             company="Zweite GmbH")
    await _tick(user)

    await user.should_see("Django Entwickler")


# --------------------------------------------------------------------------
# The rail, on every screen
# --------------------------------------------------------------------------
async def test_the_rail_states_the_corpus_it_is_standing_next_to(
        user: User, con, data_dir):
    """The bar is the answer to "the app never tells me anything": it has to
    carry real numbers on the very first paint, not after a tick."""
    _posting(con)
    _posting(con, external_id="e2", title="Django Entwickler",
             company="Zweite GmbH")
    await user.open("/")

    await user.should_see("Unterlagen")
    await user.should_see("2 neu")
    await user.should_see("2 gefunden · 2 in Arbeit")
    await user.should_see("Gmail liest mit — Phase 3")


async def test_the_rail_follows_the_engine_without_being_asked(
        user: User, con, data_dir):
    """A posting arriving in the background moves the count in the bar, from
    whichever screen he happens to be standing on."""
    await user.open("/settings")
    await user.should_see("0 gefunden · 0 in Arbeit")

    _posting(con)
    await _tick(user)

    await user.should_see("1 gefunden · 1 in Arbeit")


async def test_the_rail_marks_the_screen_he_is_on(user: User, con, data_dir):
    await user.open("/settings")
    marked = [e for e in user.client.elements.values()
              if e.props.get("data-current") == "true"]
    names = [next(d.text for d in e.descendants() if isinstance(d, ui.label))
             for e in marked]
    assert names == ["Einstellungen"], "exactly one rubric is marked, and it is this one"


async def test_the_inboxs_old_address_still_lands_on_the_postings(
        user: User, con, data_dir):
    """/jobs was the inbox for the whole of phase 1 and 2 — his own bookmarks
    point at it."""
    _posting(con)
    await user.open("/jobs")
    await user.should_see("Python Entwickler")


# --------------------------------------------------------------------------
# The Posteingang: a list, a reading pane, and the keyboard between them
# --------------------------------------------------------------------------
async def test_the_advert_is_read_before_the_machine_has_its_say(
        user: User, con, data_dir):
    """His rule, and the reason the score panel moved: he reads the posting
    first and only then sees what the app made of it."""
    job_id = _posting(con)
    db.set_job_score(con, job_id, 85, "Exakte Rollenübereinstimmung.")
    con.execute("UPDATE jobs SET description=? WHERE id=?",
                ("Wir suchen zum nächstmöglichen Zeitpunkt.", job_id))
    con.commit()
    await user.open("/")

    await user.should_see("Wir suchen zum nächstmöglichen Zeitpunkt.")
    # Elements carry increasing ids in creation order, so this is the order he
    # meets them down the page.
    advert = next(e for e in user.client.elements.values()
                  if isinstance(e, ui.markdown))
    verdict = next(e for e in user.client.elements.values()
                   if str(getattr(e, "text", "")).startswith("WARUM"))
    reasoning = next(e for e in user.client.elements.values()
                     if "Exakte Rollenübereinstimmung" in str(getattr(e, "text", "")))
    assert advert.id < verdict.id < reasoning.id, \
        "the score's reasoning stands above the advert"


async def test_the_keyboard_moves_the_selection_and_opens_what_it_lands_on(
        user: User, con, data_dir):
    """He works this list without the mouse; j and k are the whole point of a
    permanent list beside a reading pane."""
    _posting(con, title="Erste Stelle", company="Alpha GmbH")
    _posting(con, external_id="e2", title="Zweite Stelle", company="Beta GmbH")
    await user.open("/")
    await user.should_see("Erste Stelle")

    await _press(user, "j")
    await user.should_see("Zweite Stelle")
    assert len(_selected(user)) == 1

    await _press(user, "k")
    assert len(_selected(user)) == 1


async def test_x_puts_a_posting_away_and_s_sets_it_aside(user: User, con,
                                                         data_dir):
    job_id = _posting(con)
    await user.open("/")
    await user.should_see("Beispiel GmbH")

    await _press(user, "s")
    assert con.execute("SELECT bookmarked_at FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] != ""

    await _press(user, "x")
    assert con.execute("SELECT status FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] == "skipped"
    await user.should_not_see("Beispiel GmbH")


async def test_the_keyboard_keeps_out_of_the_fields_he_types_in(user: User, con,
                                                                data_dir):
    """'x' typed into the search box must not throw the posting away. Only the
    browser knows where the caret is, so this is the framework's rule and the
    page has to ask for it — the shortcuts are worse than useless without it."""
    _posting(con)
    await user.open("/")
    await user.should_see("Beispiel GmbH")
    keyboard = next(e for e in user.client.elements.values()
                    if isinstance(e, ui.keyboard))
    assert set(keyboard.props["ignore"]) >= {"input", "textarea", "select"}


async def test_reading_a_posting_marks_it_read_and_empties_neu(
        user: User, con, data_dir):
    """Selecting is reading: the row loses its unread mark and the Neu view —
    and the count in the rail — shrink by one."""
    job_id = _posting(con)
    _posting(con, external_id="e2", title="Zweite Stelle", company="Beta GmbH")
    await user.open("/")
    await user.should_see("Beispiel GmbH")
    # Opening the app selects the top row so the pane is not empty, but that
    # is a preview, not reading: nothing is consumed until he moves.
    assert con.execute("SELECT COUNT(*) FROM jobs WHERE opened_at<>''"
                       ).fetchone()[0] == 0

    await _press(user, "j")
    await _press(user, "k")

    assert con.execute("SELECT opened_at FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] != ""
    assert [e.props.get("data-unread") for e in _rows(user)] == ["false", "false"]
