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


async def _press(user: User, key: str, *, action: str = "keydown",
                 repeat: bool = False, ctrlKey: bool = False,
                 metaKey: bool = False, altKey: bool = False) -> None:
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
                "action": action, "repeat": repeat, "key": key,
                "code": f"Key{key.upper()}", "location": 0,
                "altKey": altKey, "ctrlKey": ctrlKey, "metaKey": metaKey,
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
    await user.should_see("2 Anzeigen · 2 von 2 Firmen offen")
    await user.should_see("Gmail liest mit — Phase 3")


async def test_the_rail_follows_the_engine_without_being_asked(
        user: User, con, data_dir):
    """A posting arriving in the background moves the count in the bar, from
    whichever screen he happens to be standing on."""
    await user.open("/settings")
    await user.should_see("0 Anzeigen · 0 von 0 Firmen offen")

    _posting(con)
    await _tick(user)

    await user.should_see("1 Anzeigen · 1 von 1 Firmen offen")


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


async def test_the_posting_he_opens_does_not_vanish_from_the_new_view(
        user: User, con, data_dir):
    """Found in the running app: "Neu" hides what he has read, so the moment
    the next tick landed, the posting he had just clicked dropped out of the
    list and the reading pane jumped back to the top of it."""
    top = _posting(con, title="Erste Stelle", company="Alpha GmbH")
    second = _posting(con, external_id="e2", title="Zweite Stelle",
                      company="Beta GmbH")
    db.set_job_score(con, top, 90, "sehr gut")
    db.set_job_score(con, second, 80, "gut")
    con.commit()
    await user.open("/")
    await user.should_see("Zweite Stelle")

    await _press(user, "j")            # he moves onto the second row: read
    await _tick(user)                  # …and the watcher runs

    await user.should_see("Zweite Stelle")
    assert [e.props.get("aria-selected") for e in _rows(user)] == ["false", "true"]
    assert con.execute("SELECT opened_at FROM jobs WHERE id=?",
                       (second,)).fetchone()[0] != ""


async def test_leaving_the_view_and_coming_back_is_when_it_empties(
        user: User, con, data_dir):
    top = _posting(con, title="Erste Stelle", company="Alpha GmbH")
    second = _posting(con, external_id="e2", title="Zweite Stelle",
                      company="Beta GmbH")
    db.set_job_score(con, top, 90, "sehr gut")
    db.set_job_score(con, second, 80, "gut")
    con.commit()
    await user.open("/")
    await _press(user, "j")
    await user.should_see("Zweite Stelle")

    select = next(iter(user.find(marker="view-select").elements))
    select.set_value("offen")
    await asyncio.sleep(0.3)
    select.set_value("neu")
    await asyncio.sleep(0.3)

    await user.should_not_see("Zweite Stelle")


# --------------------------------------------------------------------------
# What the review panel found in the running screen
# --------------------------------------------------------------------------
async def test_a_row_is_not_a_button_or_the_keyboard_dies_on_the_first_click(
        user: User, con, data_dir):
    """`ui.keyboard`'s `ignore` is a CLIENT-side rule reading
    document.activeElement, and a browser focuses a <button> on mousedown. With
    rows as buttons, clicking a posting to read it silently killed every
    shortcut while the strip above kept advertising them."""
    _posting(con)
    await user.open("/")
    await user.should_see("Beispiel GmbH")

    keyboard = next(e for e in user.client.elements.values()
                    if isinstance(e, ui.keyboard))
    ignored = {t.lower() for t in keyboard.props["ignore"]}
    rows = _rows(user)
    assert rows, "no rows rendered"
    for row in rows:
        assert row.tag not in ignored, (
            f"a row is a <{row.tag}>, which ui.keyboard ignores — clicking one "
            f"would switch the shortcuts off")
        assert row.props.get("role") == "option"


async def test_a_held_key_acts_once_and_not_thirty_times(user: User, con,
                                                          data_dir):
    """Auto-repeat fires about thirty events a second. Holding ⏎ on a posting
    whose channel is unresolved would fire a burst of concurrent resolutions at
    one employer's host."""
    job_id = _posting(con)
    await user.open("/")
    await user.should_see("Beispiel GmbH")

    await _press(user, "x", repeat=True)

    assert con.execute("SELECT status FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] == "new"


@pytest.mark.parametrize("modifier", ["ctrlKey", "metaKey", "altKey"])
async def test_a_shortcut_with_a_modifier_is_the_browsers_business(
        user: User, con, data_dir, modifier):
    job_id = _posting(con)
    await user.open("/")
    await user.should_see("Beispiel GmbH")

    await _press(user, "x", **{modifier: True})

    assert con.execute("SELECT status FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] == "new"


async def test_a_key_release_does_not_act_a_second_time(user: User, con,
                                                        data_dir):
    """NiceGUI registers keydown AND keyup; without the guard every shortcut
    fires twice — one j moves two rows, one s toggles a bookmark on and off."""
    job_id = _posting(con)
    await user.open("/")
    await user.should_see("Beispiel GmbH")

    await _press(user, "s", action="keyup")

    assert con.execute("SELECT bookmarked_at FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] == ""


async def test_a_key_under_an_open_dialog_never_reaches_the_list(
        user: User, con, data_dir, monkeypatch):
    """Reaching for the close button and hitting 'x' skipped the posting behind
    the dialog — and the refresh rebuilt the list beneath the still-open
    dialog, so nothing on screen flickered."""
    from jobdeck.ui.pages import jobs as jobs_page

    async def finished_draft(job_id):
        return {"ok": True, "error": "", "draft": {
            "recipient": "jobs@beispiel.example", "betreff": "Bewerbung",
            "email_body": "…", "anschreiben_body": "…", "llm_model": "stub",
            "pdf_path": ""}}

    monkeypatch.setattr(jobs_page.drafting, "draft_for_job", finished_draft)
    job_id = _emailable(con)
    await user.open("/")
    user.find("Bewerbung per E-Mail erstellen").click()
    await asyncio.sleep(0.3)
    await user.should_see("Entwurf — Python Entwickler")

    await _press(user, "x")

    assert con.execute("SELECT status FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] == "new"


async def test_x_lands_on_the_row_that_took_its_place(user: User, con, data_dir):
    """Jumping back to the top made triaging a fifty-row page cost O(n²)
    keystrokes, on the screen whose stated purpose is keyboard triage."""
    ids = []
    for n, score in enumerate((90, 80, 70)):
        job_id = _posting(con, external_id=f"e{n}", title=f"Stelle {n}",
                          company=f"Firma {n}")
        db.set_job_score(con, job_id, score, "passt")
        ids.append(job_id)
    con.commit()
    await user.open("/")
    await user.should_see("Stelle 0")

    await _press(user, "j")            # onto the middle row
    await _press(user, "x")            # …and away with it

    await user.should_not_see("Stelle 1")
    selected = _selected(user)
    assert len(selected) == 1
    names = [str(getattr(d, "text", "")) for d in selected[0].descendants()]
    assert "Firma 2" in names, f"landed on {names} instead of the next row"


async def test_a_tick_that_changes_nothing_here_redraws_nothing(
        user: User, con, data_dir):
    """The signature is global by design, so a score landing on a posting he is
    not looking at moves it. Redrawing anyway empties two scroll containers and
    loses his place in a two-page advert."""
    _posting(con)
    await user.open("/")
    await user.should_see("Beispiel GmbH")
    row, reader_text = _rows(user)[0], None
    reader_text = next(e for e in user.client.elements.values()
                       if isinstance(e, ui.markdown))

    # a write that moves the shared signature without touching this page
    db.record_llm_usage(con, input_tokens=10, output_tokens=5, cost_usd=0.01)
    db.add_bewerbung(con, {"firma": "Ganz Andere GmbH", "status": "Gesendet"})
    con.commit()
    await _tick(user)

    assert _rows(user)[0] is row, "the list was rebuilt for nothing"
    assert next(e for e in user.client.elements.values()
                if isinstance(e, ui.markdown)) is reader_text, \
        "the advert he is reading was rebuilt for nothing"


async def test_a_posting_that_falls_out_of_the_view_stays_in_the_reader(
        user: User, con, data_dir):
    """Scored 0 while he reads it: the old code silently retargeted the pane to
    a different posting mid-paragraph."""
    job_id = _posting(con)
    other = _posting(con, external_id="e2", title="Zweite Stelle",
                     company="Beta GmbH")
    db.set_job_score(con, job_id, 90, "passt")      # so he lands on this one
    db.set_job_score(con, other, 70, "passt weniger")
    con.commit()
    await user.open("/")
    await user.should_see("Beispiel GmbH")

    db.set_job_score(con, job_id, 0, "harte Anforderung verletzt")
    con.commit()
    await _tick(user)

    await user.should_see("Beispiel GmbH")
    await user.should_see("aus dieser Ansicht gefallen")


async def test_the_other_postings_of_a_company_are_listed_and_openable(
        user: User, con, data_dir):
    """One row stands for a company, which makes choosing WHICH posting to
    apply with a real decision — and the row alone gives him nothing to decide
    with. One employer holds 27 of his postings."""
    best = _posting(con, title="Beste Stelle", company="Eine GmbH")
    other = _posting(con, external_id="e2", title="Andere Stelle",
                     company="Eine GmbH")
    db.set_job_score(con, best, 90, "passt")
    db.set_job_score(con, other, 70, "passt weniger")
    con.commit()
    await user.open("/")

    await user.should_see("Beste Stelle")
    await user.should_see("1 weitere Stelle bei Eine GmbH")
    await user.should_see("Andere Stelle")       # by title, in the reader
    await user.should_see("öffnen")              # with its own link
    await user.should_not_see("Top 10")


# --------------------------------------------------------------------------
# The rendered contract: what a mutation would have to break
# --------------------------------------------------------------------------
def _classed(user: User, css: str):
    return [e for e in user.client.elements.values()
            if css in getattr(e, "_classes", [])]


async def test_the_rail_really_draws_its_bars_budget_and_pulse(
        user: User, con, data_dir):
    """Every one of these was deletable or hardcodable with the suite green:
    the rail was unit-tested as pure functions and never rendered once."""
    db.add_profile(con, {"name": "Python", "keywords": "python"})
    _posting(con)
    db.set_setting(con, "daily_send_cap", "5")
    con.commit()
    await user.open("/")

    bars = _classed(user, "jd-track")
    assert len(bars) == 5, "one proportional bar per rubric"
    widths = [b.default_slot.children[0]._style.get("width") for b in bars]
    assert any(w and w not in ("0%", None) for w in widths), "every bar is empty"

    assert len(_classed(user, "jd-budget-box")) == 5, "today's budget"
    assert len(_classed(user, "jd-pulse-dot")) == 3, "the engine's three beats"
    await user.should_see("Heute gesendet")
    await user.should_see("Puls")


async def test_the_pulse_dot_shows_which_state_it_is_in(user: User, con,
                                                        data_dir):
    """Dropping `{beat.state}` from the class made run, ok and idle identical
    — the rail's whole claim is that you can see the engine working."""
    _posting(con)
    con.commit()
    await user.open("/")
    states = {c for dot in _classed(user, "jd-pulse-dot")
              for c in dot._classes if c in ("run", "ok", "idle")}
    assert states, "no dot carries a state at all"


async def test_a_rubric_really_navigates(user: User, con, data_dir):
    """The rail is the app's ONLY navigation since the old drawer went. With
    the click handler gone he is stranded on whatever page he loaded."""
    await user.open("/")
    rubrics = [e for e in user.client.elements.values()
               if "jd-sec" in getattr(e, "_classes", [])]
    assert len(rubrics) == 5
    live_ones = [r for r in rubrics if r.props.get("data-enabled") == "true"]
    assert len(live_ones) == 4, "only Antworten is meant to be a dead end"
    for rubric in live_ones:
        assert any(listener.type == "click"
                   for listener in rubric._event_listeners.values()), \
            "a rubric with no handler strands him on whatever page he loaded"


async def test_the_pager_is_rendered_when_there_is_a_second_page(
        user: User, con, data_dir, monkeypatch):
    """Deleting `render_pager(view)` made 148 of his 198 working companies
    unreachable with the suite green — the exact defect the honest-inbox slice
    was written to fix."""
    from jobdeck.ui.pages import jobs as jobs_page
    monkeypatch.setattr(jobs_page, "PAGE_SIZE", 2)
    for n in range(5):
        job_id = _posting(con, external_id=f"e{n}", title=f"Stelle {n}",
                          company=f"Firma {n}")
        db.set_job_score(con, job_id, 90 - n, "passt")
    con.commit()
    await user.open("/")

    await user.should_see("Seite 1/3")


async def test_a_refused_action_renders_disabled_and_says_why(
        user: User, con, data_dir):
    """Both the disabled state and the reason beside the button could be
    deleted with the suite green, and the only rendered string that could have
    caught it is produced by reader_notes as well."""
    _posting(con, company="Beispiel GmbH")
    db.add_bewerbung(con, {"gesendet_am": "2026-06-12", "firma": "Beispiel GmbH",
                           "email": "", "kanal": "E-Mail", "status": "Absage"})
    con.commit()
    await user.open("/")
    select = next(iter(user.find(marker="view-select").elements))
    select.set_value("firma_kontaktiert")
    await asyncio.sleep(0.3)

    await user.should_see("Bewerbung nicht möglich")
    button = next(e for e in user.client.elements.values()
                  if isinstance(e, ui.button) and e.text == "Bewerbung nicht möglich")
    assert button.enabled is False, "the refusal is offered as a live button"
    reasons = _classed(user, "jd-reason")
    assert reasons and "bereits beworben" in reasons[0].text


async def test_the_warnings_stand_above_the_advert_on_the_screen(
        user: User, con, data_dir):
    """Moving the notes below the markdown would bury "already applied",
    "offline" and "Arbeitnehmerüberlassung" under a wall of prose, and the
    ordering test only pinned the score block."""
    job_id = _posting(con)
    db.set_job_liveness(con, job_id, "gone")
    con.execute("UPDATE jobs SET description=? WHERE id=?",
                ("Wir suchen zum nächstmöglichen Zeitpunkt.", job_id))
    con.commit()
    await user.open("/")
    select = next(iter(user.find(marker="view-select").elements))
    select.set_value("offline")     # a dead ad leaves the working list
    await asyncio.sleep(0.3)

    await user.should_see("Anzeige offline")
    warning = next(e for e in _classed(user, "jd-note")
                   if "offline" in str(getattr(e, "text", "")))
    advert = next(e for e in user.client.elements.values()
                  if isinstance(e, ui.markdown))
    assert warning.id < advert.id, "the warning is buried under the advert"


async def test_the_search_box_really_filters_and_waits_for_him_to_finish(
        user: User, con, data_dir):
    """Nothing tested the arm at all — deleting it, making it prefix-only or
    dropping the company half all kept the suite green."""
    _posting(con, title="Django Entwickler", company="Alpha GmbH")
    _posting(con, external_id="e2", title="Java Entwickler", company="Beta GmbH")
    await user.open("/")
    await user.should_see("Java Entwickler")

    box = next(e for e in user.client.elements.values() if isinstance(e, ui.input))
    assert "debounce" in box.props, "a reload per keystroke"
    box.set_value("Django")
    await asyncio.sleep(0.3)

    await user.should_see("Django Entwickler")
    await user.should_not_see("Java Entwickler")
    await user.should_see("gefiltert nach „Django“")
