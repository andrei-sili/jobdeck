"""Pages that keep up with the engine, rendered for real.

The mechanism's decision table is unit-tested in test_live.py; what these prove
is the wiring — that a page really rebuilds itself when the background writes
something, and really holds off while he is reading a posting. Both are
invisible to any data-layer test: the whole defect class was "the query is
right, nothing asks it again".
"""

import asyncio
import datetime
import sys

import pytest
from nicegui import background_tasks, ui
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


# A real advert, because two of these tests hold the RENDERED advert as their
# handle on the reading pane — and a posting with no text does not draw one any
# more, it draws the sentence saying there is none. A fixture with no advert
# was quietly testing that sentence and calling it "the advert he is reading".
_ADVERT = ("Wir suchen eine Python-Entwicklerin. Django, FastAPI, "
           "PostgreSQL, Docker. Bewerbung bitte per E-Mail. ") * 8


def _posting(con, external_id="e1", title="Python Entwickler",
             company="Beispiel GmbH", description=_ADVERT):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": external_id, "title": title,
        "company": company, "url": "https://beispiel.example/1",
        "description": description,
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
    running_before = set(background_tasks.running_tasks)
    open_dialogs_before = {
        id(element)
        for element in user.client.elements.values()
        if isinstance(element, ui.dialog) and element.value
    }
    with user.client:
        keyboard._handle_key(GenericEventArguments(
            sender=keyboard, client=user.client, args={
                "action": action, "repeat": repeat, "key": key,
                "code": f"Key{key.upper()}", "location": 0,
                "altKey": altKey, "ctrlKey": ctrlKey, "metaKey": metaKey,
                "shiftKey": False}))
    handlers = set(background_tasks.running_tasks) - running_before
    if handlers:
        done, pending = await asyncio.wait(handlers, timeout=2)
        for task in done:
            task.result()
        opened_dialog = any(
            isinstance(element, ui.dialog)
            and element.value
            and id(element) not in open_dialogs_before
            for element in user.client.elements.values()
        )
        if pending and not opened_dialog:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise TimeoutError("keyboard handler did not finish or open a dialog")


def _ancestors(element):
    """Every parent of an element, up to the page."""
    node = element
    while node is not None:
        node = getattr(node, "parent_slot", None)
        node = getattr(node, "parent", None) if node is not None else None
        if node is not None:
            yield node


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
    await user.open("/bewerbungen")
    await user.should_see("Keine Bewerbung passt zu dieser Suche.")

    db.add_bewerbung(con, {"gesendet_am": "2026-08-11", "firma": "Beispiel GmbH",
                           "email": "hr@beispiel.example", "kanal": "E-Mail",
                           "status": "Gesendet"})
    con.commit()
    await _tick(user)

    await user.should_see("Beispiel GmbH")


async def test_the_profile_list_shows_a_poll_that_just_happened(
        user: User, con, data_dir):
    profile_id = db.add_profile(con, {"name": "Python", "keywords": "python"})
    con.commit()
    await user.open("/unterlagen")
    await user.should_see("Python")

    db.mark_profile_polled(con, profile_id, error="jooble: 401 Unauthorized")
    con.commit()
    await _tick(user)

    await user.should_see("401 Unauthorized")


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
    user.find("E-Mail-Bewerbung schreiben").click()
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
    user.find("E-Mail-Bewerbung schreiben").click()
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
    was fired from. A stale busy marker would defer every later update for the
    remainder of the page visit."""
    _posting(con)
    await user.open("/")
    await user.should_see("Python Entwickler")

    user.find("✕ Firma ausblenden").click()
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
    # no token in the test data dir: the Antworten rubric must say the
    # reader's real precondition, not pretend an empty inbox
    await user.should_see("Gmail ist nicht verbunden")


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


async def test_x_hides_the_company_and_s_sets_a_posting_aside(user: User, con,
                                                              data_dir):
    """`x` reaches the COMPANY now. Measured before the change: eleven presses,
    eleven different companies — putting one advert away had never once helped
    him, while one staffing agency held seven under seven branch names."""
    job_id = _posting(con)
    await user.open("/")
    await user.should_see("Beispiel GmbH")

    await _press(user, "s")
    assert con.execute("SELECT bookmarked_at FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] != ""

    await _press(user, "x")
    assert [r["company"] for r in db.list_hidden_companies(con)] == \
        ["Beispiel GmbH"]
    # …and the posting itself is untouched: it is a view, not a status
    assert con.execute("SELECT status FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] == "new"
    assert db.count_job_groups(con, "new", hidden="exclude") == 0


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
    user.find("E-Mail-Bewerbung schreiben").click()
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
    assert len(live_ones) == 5, "since Phase 3 every rubric opens a screen"
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
    db.add_bewerbung(con, {
        "gesendet_am": (datetime.date.today()
                        - datetime.timedelta(days=5)).isoformat(),
        "firma": "Beispiel GmbH", "email": "", "kanal": "E-Mail",
        "status": "Absage"})
    con.commit()
    await user.open("/")
    select = next(iter(user.find(marker="view-select").elements))
    select.set_value("firma_kontaktiert")
    await asyncio.sleep(0.3)

    steps = [e for e in user.client.elements.values()
             if isinstance(e, ui.button)
             and any(word in e.text for word in
                     ("schreiben", "Bewerbung starten", "Abgeschickt",
                      "eintragen", "senden"))]
    assert steps, "no apply step is rendered at all"
    assert all(not b.enabled for b in steps), \
        "an application that cannot happen is offered as a live button"
    reasons = _classed(user, "jd-reason")
    assert reasons and any("zurückgestellt" in r.text for r in reasons)


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


async def test_the_daily_probe_pass_does_not_rebuild_the_advert_he_is_reading(
        user: User, con, data_dir):
    """The liveness pass re-stamps hundreds of rows in a couple of minutes.
    Every one of those ticks used to rebuild the reading pane along with the
    list, because the probe stamp sat in the row's fingerprint while nothing on
    screen shows it for a posting that is still online."""
    job_id = _posting(con)
    other = _posting(con, external_id="e2", title="Zweite Stelle",
                     company="Beta GmbH")
    db.set_job_score(con, job_id, 90, "passt")
    db.set_job_score(con, other, 70, "auch")
    con.commit()
    await user.open("/")
    await user.should_see("Beispiel GmbH")
    advert = next(e for e in user.client.elements.values()
                  if isinstance(e, ui.markdown))

    db.set_job_liveness(con, other, "alive")     # the pass, on another row
    con.commit()
    await _tick(user)

    assert next(e for e in user.client.elements.values()
                if isinstance(e, ui.markdown)) is advert, \
        "the advert he is reading was rebuilt by a probe on another posting"


# --------------------------------------------------------------------------
# Applying without leaving the screen he reads on
# --------------------------------------------------------------------------
async def test_a_form_posting_carries_its_whole_toolkit_in_the_reader(
        user: User, con, data_dir):
    """His complaint, in one assertion: everything an application needs, on
    the screen where he reads the posting."""
    job_id = _posting(con)
    db.set_apply_channel(con, job_id, "ats_form", "JOIN",
                         "https://join.com/companies/x/1/apply")
    con.commit()
    await user.open("/")

    # two controls, not four — and the price is on the one that spends
    await user.should_see("Bewerbung starten · ~0,09 $")
    await user.should_see("Abgeschickt")
    await user.should_not_see("Mappe erstellen")
    await user.should_not_see("Kanal ermitteln")


async def test_recording_from_the_reader_makes_the_posting_leave_for_good(
        user: User, con, data_dir):
    """The detour that started this: he filled a form on the employer's site
    and there was no way to record it from here at all."""
    job_id = _posting(con)
    other = _posting(con, external_id="e2", title="Zweite Stelle",
                     company="Beta GmbH")
    db.set_job_score(con, job_id, 90, "passt")     # so he lands on this one
    db.set_job_score(con, other, 70, "auch")
    db.mark_form_opened(con, job_id)   # recording follows a start, never precedes it
    con.commit()
    await user.open("/")
    await user.should_see("Beispiel GmbH")

    user.find("Abgeschickt", kind=ui.button).click()
    await asyncio.sleep(0.4)
    # it records at once and names what it did, with the way back beside it
    await user.should_see("Rückgängig")

    row = con.execute("SELECT status, bewerbung_id FROM jobs WHERE id=?",
                      (job_id,)).fetchone()
    assert row[0] == "applied" and row[1] is not None
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 1
    # gone from the list, and gone from the reading pane — not left behind
    # under a "it fell out of this view" note, because he asked for it.
    # Asserted on the ROWS: a closed dialog stays alive as an element, so the
    # company name is still somewhere in the tree.
    def listed():
        return [d.text for row in _rows(user) for d in row.descendants()
                if isinstance(d, ui.label)]

    assert not any("Beispiel GmbH" in t for t in listed())
    reader = [e.text for e in user.client.elements.values()
              if isinstance(e, ui.label) and "gefallen" in str(e.text)]
    assert reader == [], "it was left in the reading pane behind a note"

    # …and it stays gone when the list re-reads itself
    await _tick(user)
    assert not any("Beispiel GmbH" in t for t in listed())


async def test_opening_the_form_records_that_the_application_started(
        user: User, con, data_dir, monkeypatch):
    """Opening an employer's form is the moment an application begins, and the
    app stamps it. The posting KEEPS its place in the list — losing the advert
    he had just started working on is the complaint this replaced."""
    job_id = _posting(con)
    db.set_apply_channel(con, job_id, "ats_form", "JOIN",
                         "https://join.com/companies/x/1/apply")
    con.commit()
    await user.open("/")

    # the navigation itself is the browser's business; what this pins is that
    # the app records the application as started before handing it over
    opened = []
    monkeypatch.setattr(ui.navigate, "to",
                        lambda url, **kw: opened.append(url))
    user.find("Bewerbung starten").click()
    await asyncio.sleep(0.4)

    assert con.execute("SELECT form_opened_at FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] != ""
    assert opened == ["https://join.com/companies/x/1/apply"]
    # and it is still in the working list, under his cursor
    assert db.get_job(con, job_id)["status"] == "new"
    await user.should_see("Beispiel GmbH")


async def test_a_started_form_appears_on_the_strip_and_stays_there(
        user: User, con, data_dir):
    """He opened six forms in thirteen minutes and the list moved under him
    every time. The strip is a sibling of the scroll container, so nothing the
    list does — a search, a view, a page — can take it away."""
    job_id = _posting(con)
    await user.open("/")
    await user.should_see("Beispiel GmbH")

    # written the way another tab's press writes it
    db.mark_form_opened(con, job_id)
    con.commit()
    await _tick(user)

    await user.should_see("Formular bei Beispiel GmbH")
    await user.should_see("Abgeschickt")
    # …and it says the documents are not ready, because they are not
    await user.should_see("Mappe NICHT fertig")


async def test_the_strip_gets_older_without_redrawing_the_page(
        user: User, con, data_dir, monkeypatch):
    """The relabel runs on every tick, including while he is reading — so it
    must be `set_text` and never a rebuild. A rebuild here empties both scroll
    containers and takes the advert away from him mid-sentence."""
    import datetime

    from jobdeck.ui.pages import jobs as jobs_page
    job_id = _posting(con)
    con.execute("UPDATE jobs SET form_opened_at=? WHERE id=?",
                ((datetime.datetime.now()
                  - datetime.timedelta(minutes=5)).isoformat(), job_id))
    con.commit()
    await user.open("/")
    # it reports its age first — the label has to CHANGE, or the assertion
    # below passes on a strip that was already saying the right thing
    await user.should_see("seit 5 Min.")
    row = _rows(user)[0]
    entry = next(e for e in user.client.elements.values()
                 if isinstance(e, ui.label)
                 and "Formular bei Beispiel GmbH" in str(e.text))

    # time passes. The THRESHOLD is moved rather than the stamp, because the
    # stamp is what never changes in production — moving it would test a write
    # the app does not make, and the beat reads the row it already holds.
    monkeypatch.setattr(jobs_page, "ASK_AFTER_MIN", 1)
    await _tick(user)

    # the question is on screen …
    await user.should_see("abgeschickt?")
    # … carried by the SAME element, which is what "set_text, never a rebuild"
    # means; a re-rendered strip would be a new element with the same words
    assert entry.text.endswith("abgeschickt?")
    assert entry.id in user.client.elements, "the strip element was replaced"
    # … and the list was not touched either
    assert _rows(user)[0] is row, "the strip rebuilt the page to change a label"


async def test_recording_from_the_strip_closes_the_loop(
        user: User, con, data_dir):
    """The second of the two presses. The entry leaves because the application
    is written, not because anything expired."""
    job_id = _posting(con)
    db.mark_form_opened(con, job_id)
    con.commit()
    await user.open("/")
    await user.should_see("Formular bei Beispiel GmbH")

    # the STRIP's button, not the reading pane's — they carry the same label,
    # so finding by text alone would be satisfied by either and the strip's
    # could be deleted with this test still green
    # The button that does this belongs to the STRIP, not to the reading pane —
    # they carry the same label, so a test that only searched by text would be
    # satisfied by either and the strip's control could be deleted with the
    # suite green.
    on_strip = [e for e in user.client.elements.values()
                if isinstance(e, ui.button) and "Abgeschickt" in str(e.text)
                and any("jd-laeuft-row" in getattr(p, "_classes", [])
                        for p in _ancestors(e))]
    assert len(on_strip) == 1, "the running application has no Abgeschickt of its own"

    user.find("Abgeschickt", kind=ui.button).click()
    await asyncio.sleep(0.5)

    assert db.get_job(con, job_id)["status"] == "applied"
    assert con.execute(
        "SELECT kanal FROM bewerbungen").fetchone()[0] == "Online-Portal"
    labels = [str(e.text) for e in user.client.elements.values()
              if isinstance(e, ui.label)]
    assert not any("Formular bei Beispiel GmbH" in t for t in labels)


async def test_a_failed_letter_says_the_mappe_is_not_complete(
        user: User, con, data_dir, monkeypatch):
    """The legacy flow makes a failed complete-package build visible.

    The employer tab still opens when document preparation fails. ADR 0005
    defines the target versioned document model.
    """
    from jobdeck.services import drafting, mappe
    job_id = _posting(con)
    db.set_apply_channel(con, job_id, "ats_form", "JOIN",
                         "https://join.com/companies/x/1/apply")
    con.commit()
    await user.open("/")
    opened = []
    monkeypatch.setattr(ui.navigate, "to", lambda url, **kw: opened.append(url))

    async def no_letter(_job_id):
        return {"ok": False, "error": "ANTHROPIC_API_KEY is not set",
                "draft": None}

    async def never(_job_id):
        raise AssertionError("the Mappe was built without a letter")

    monkeypatch.setattr(drafting, "draft_for_job", no_letter)
    monkeypatch.setattr(mappe, "create_mappe", never)

    user.find("Bewerbung starten", kind=ui.button).click()
    await asyncio.sleep(0.6)

    assert opened == ["https://join.com/companies/x/1/apply"]
    await user.should_see("NICHT vollständig")
    job = db.get_job(con, job_id)
    assert job["form_opened_at"] != "", "the application still started"
    assert job["mappe_kind"] == "", "nothing complete is staged, and it says so"
    await user.should_see("Mappe NICHT fertig")     # the strip says it too


async def test_the_daily_letter_limit_is_enforced_where_the_money_is_spent(
        user: User, con, data_dir):
    """A screen that only greys out a button is a screen the keyboard, the
    batch and a second tab all walk past. The refusal lives in the same
    transaction that commits the spend."""
    from jobdeck.services import drafting
    job_id = _posting(con)
    db.set_setting(con, "daily_draft_cap", "0")
    con.commit()

    refusal = await asyncio.to_thread(drafting._claim, job_id)

    assert "limit" in refusal and "Einstellungen" in refusal
    assert db.get_draft_by_job(con, job_id) is None, "a claim was taken anyway"

    await user.open("/")
    await user.should_see("Tageslimit")


async def test_the_whole_press_runs_and_records_nothing(
        user: User, con, data_dir, monkeypatch):
    """The SUCCESS half of the press, executed end to end.

    Every other functional test stops at the letter, because drafting fails
    without an API key — so everything after it was unexecuted code, and a
    recorder placed there would have gone unnoticed by 1338 tests. Both halves
    are stubbed here so the rest actually runs."""
    from jobdeck.services import drafting, mappe
    job_id = _posting(con)
    db.set_apply_channel(con, job_id, "ats_form", "JOIN", "https://join.com/x/apply")
    con.commit()
    await user.open("/")
    monkeypatch.setattr(ui.navigate, "to", lambda url, **kw: None)

    async def letter(jid):
        with db.db() as c:
            db.upsert_draft(c, jid, {"status": "ready",
                                     "anschreiben_body": "Sehr geehrte Damen,"})
            c.commit()
        return {"ok": True, "error": "", "draft": {"id": 1}}

    async def built(jid):
        with db.db() as c:
            db.set_upload(c, jid, "/tmp/Bewerbung.pdf", "vollständig")
            c.commit()
        return {"ok": True, "error": "", "pdf_path": "/tmp/Bewerbung.pdf",
                "warning": "", "pages": 10, "size_bytes": 2_100_000,
                "size_before_bytes": 3_700_000, "compression": "", "anlagen": []}

    opened = []
    monkeypatch.setattr(drafting, "draft_for_job", letter)
    monkeypatch.setattr(mappe, "create_mappe", built)
    from jobdeck.ui import helpers
    monkeypatch.setattr(helpers, "open_in_system",
                        lambda path: opened.append(str(path)))

    user.find("Bewerbung starten", kind=ui.button).click()
    await asyncio.sleep(0.8)

    job = db.get_job(con, job_id)
    assert job["form_opened_at"] != ""
    assert job["mappe_kind"] == "vollständig"
    # the folder is opened for him — the folder, because that is what teaches
    # the file dialog where to land
    assert opened and opened[0].endswith("Bewerbung-hochladen")
    # and NOTHING was recorded: the app cannot see whether he pressed submit
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 0
    assert job["status"] == "new"
    await user.should_see("Mappe bereit")


async def test_the_editor_the_queue_uses_opens_over_the_list_too(
        user: User, con, data_dir):
    """One editor, not two: it is the last screen before a message leaves."""
    job_id = _emailable(con)
    db.upsert_draft(con, job_id, {
        "status": "ready", "recipient": "jobs@beispiel.example",
        "betreff": "Bewerbung als Python Entwickler",
        "email_body": "Sehr geehrte Damen und Herren,"})
    con.commit()
    await user.open("/")

    user.find("Prüfen und senden").click()
    await asyncio.sleep(0.4)

    await user.should_see("Bewerbung als Python Entwickler")
    await user.should_see("Jetzt senden")


async def test_an_application_landing_while_the_editor_is_open_reaches_the_confirmation(
        user: User, con, data_dir):
    """The last statement before a real send. Read when the confirmation is
    BUILT, not when the editor opened: an auto-send tick or a second tab can
    write that company's application while the dialog sits there."""
    job_id = _emailable(con)
    db.upsert_draft(con, job_id, {
        "status": "ready", "recipient": "jobs@beispiel.example",
        "betreff": "Bewerbung als Python Entwickler",
        "email_body": "Sehr geehrte Damen und Herren,"})
    db.set_setting(con, "test_recipient", "probe@example.org")
    con.commit()
    await user.open("/")
    user.find("Prüfen und senden", kind=ui.button).click()
    await asyncio.sleep(0.4)
    await user.should_see("Jetzt senden")

    # …and now somebody else applies at that company
    db.add_bewerbung(con, {"gesendet_am": "2026-08-12", "firma": "Beispiel GmbH",
                           "email": "", "kanal": "E-Mail", "status": "Gesendet"})
    con.commit()

    user.find("Jetzt senden", kind=ui.button).click()
    await asyncio.sleep(0.4)

    await user.should_see("Diese Bewerbung abschicken?")
    await user.should_see("zurückgestellt")


async def test_the_way_back_is_on_screen_the_second_after_the_form_opens(
        user: User, con, data_dir, monkeypatch):
    """The moment he wants the undo is the second after the click."""
    job_id = _posting(con)
    db.set_apply_channel(con, job_id, "ats_form", "JOIN",
                         "https://join.com/companies/x/1/apply")
    con.commit()
    await user.open("/")
    # patched AFTER the page is open: the user fixture navigates too
    monkeypatch.setattr(ui.navigate, "to", lambda url, **kw: None)

    user.find("Bewerbung starten", kind=ui.button).click()
    await asyncio.sleep(0.4)

    await user.should_see("zurück in die Arbeitsliste")
    assert con.execute("SELECT form_opened_at FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] != ""


async def test_the_reader_redraws_when_a_form_is_started_somewhere_else(
        user: User, con, data_dir):
    """The press itself refreshes by force, so it proves nothing about the
    fingerprint. What does is a write this page did NOT make: a second tab, or
    the background staging the Mappe seconds after he left for the employer.

    `refresh` skips the redraw when the row's fingerprint is unchanged, so a
    field the pane STATES and the fingerprint omits is never drawn again — the
    pane goes on offering "kein Interesse" beside an application already under
    way, and every functional test stays green."""
    job_id = _posting(con)
    db.set_apply_channel(con, job_id, "ats_form", "JOIN",
                         "https://join.com/companies/x/1/apply")
    con.commit()
    await user.open("/")
    await user.should_see("Firma ausblenden")

    # exactly what another tab's press writes — nothing on THIS page ran
    db.mark_form_opened(con, job_id)
    con.commit()
    await _tick(user)

    await user.should_see("zurück in die Arbeitsliste")


async def test_undoing_an_opened_form_asks_what_actually_happened(
        user: User, con, data_dir, monkeypatch):
    """`portal` is the app's ONLY record that an application at that company
    may already be out, and an unrecorded form submission is invisible to every
    gate. Pressing this after actually applying would erase the one hint."""
    job_id = _posting(con)
    db.set_apply_channel(con, job_id, "ats_form", "JOIN",
                         "https://join.com/companies/x/1/apply")
    con.commit()
    await user.open("/")
    monkeypatch.setattr(ui.navigate, "to", lambda url, **kw: None)
    user.find("Bewerbung starten", kind=ui.button).click()
    await asyncio.sleep(0.4)

    user.find("zurück in die Arbeitsliste", kind=ui.button).click()
    await asyncio.sleep(0.3)
    await user.should_see("hast du dich beworben?")

    # "yes" hands it to the one recorder
    user.find("Ja, eintragen", kind=ui.button).click()
    await asyncio.sleep(0.5)

    assert con.execute("SELECT status FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] == "applied"
    assert con.execute("SELECT kanal FROM bewerbungen").fetchone()[0] \
        == "Online-Portal"


async def test_a_recorded_application_can_be_taken_straight_back(
        user: User, con, data_dir):
    """The confirmation dialog is gone — he makes this press eight times an
    evening and a dialog on it is one he learns to click through. The undo is
    what pays for that, so it has to be REAL: a half-undo leaves the company
    marked as applied-to and permanently spends its only application slot.

    Asserted on the database, not on the button text: a label saying
    "Rückgängig gemacht" beside a row that is still there proves nothing."""
    job_id = _posting(con)
    db.mark_form_opened(con, job_id)
    con.commit()
    history_before = con.execute(
        "SELECT COUNT(*) FROM status_history").fetchone()[0]
    await user.open("/")
    await user.should_see("Beispiel GmbH")

    user.find("Abgeschickt", kind=ui.button).click()
    await asyncio.sleep(0.4)
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 1

    user.find("Rückgängig", kind=ui.button).click()
    await asyncio.sleep(0.5)

    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM status_history").fetchone()[0] == history_before
    row = con.execute("SELECT status, bewerbung_id FROM jobs WHERE id=?",
                      (job_id,)).fetchone()
    assert row[0] == "new" and row[1] is None
    # and it is back on screen, where he can act on it again
    await user.should_see("Beispiel GmbH")


async def test_a_refused_application_offers_no_undo_at_all(
        user: User, con, data_dir):
    """Nothing was written, so there is no earlier state an undo could restore
    — offering one would restore a state that never existed.

    And the posting stays `new`: the hold is a window, not a verdict, so
    filing it as `duplicate` would mean waiting the window out never brings it
    back. See `docs/adr/0010-company-cooling-off-window.md`."""
    job_id = _posting(con)
    db.mark_form_opened(con, job_id)
    con.commit()
    await user.open("/")
    await user.should_see("Beispiel GmbH")
    # the blocking application lands AFTER the row is on screen — a send from
    # another tab, which is exactly when he would press this by mistake
    recent = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
    db.add_bewerbung(con, {"gesendet_am": recent, "firma": "Beispiel GmbH",
                           "kanal": "E-Mail", "status": "Gesendet"})
    con.commit()

    user.find("Abgeschickt", kind=ui.button).click()
    await asyncio.sleep(0.4)

    await user.should_see("zurückgestellt")
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 1
    assert db.get_job(con, job_id)["status"] == "new"
    with pytest.raises(AssertionError):
        user.find("Rückgängig", kind=ui.button)


async def test_saying_no_puts_it_back_without_recording_anything(
        user: User, con, data_dir, monkeypatch):
    job_id = _posting(con)
    db.set_apply_channel(con, job_id, "ats_form", "JOIN",
                         "https://join.com/companies/x/1/apply")
    con.commit()
    await user.open("/")
    monkeypatch.setattr(ui.navigate, "to", lambda url, **kw: None)
    user.find("Bewerbung starten", kind=ui.button).click()
    await asyncio.sleep(0.4)

    user.find("zurück in die Arbeitsliste", kind=ui.button).click()
    await asyncio.sleep(0.3)
    user.find("Nein, zurück in die Liste", kind=ui.button).click()
    await asyncio.sleep(0.4)

    assert con.execute("SELECT status FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] == "new"
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 0


async def test_enter_on_a_started_form_never_records_on_one_keystroke(
        user: User, con, data_dir):
    """The press this slice created and the guard it removed, together.

    Once a form is started, the next step under ⏎ is "Abgeschickt" — which
    writes into the very table the duplicate gate reads. The button has ten
    seconds of "Rückgängig" beside it; a keystroke he did not mean to make has
    nobody watching for it, and he moves through this list with j and ⏎."""
    job_id = _posting(con)
    db.set_apply_channel(con, job_id, "ats_form", "JOIN", "https://join.com/x/apply")
    db.mark_form_opened(con, job_id)
    con.commit()
    await user.open("/")
    await user.should_see("Beispiel GmbH")

    await _press(user, "Enter")

    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 0, \
        "one keystroke spent that company's only application slot"
    await user.should_see("Bewerbung eintragen?")

    # and it really records once he says so
    user.find("Eintragen", kind=ui.button).click()
    await asyncio.sleep(0.5)
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 1
    assert db.get_job(con, job_id)["status"] == "applied"


async def test_x_on_a_started_form_asks_instead_of_erasing_the_record(
        user: User, con, data_dir):
    """`x` used to be a no-op here for free: a started form had left status
    'new'. Since v10 it has not, so one keystroke would move it to 'skipped' —
    off the strip, and the strip is the app's ONLY record that an application
    may already be out at that company."""
    job_id = _posting(con)
    db.set_apply_channel(con, job_id, "ats_form", "JOIN", "https://join.com/x/apply")
    db.mark_form_opened(con, job_id)
    con.commit()
    await user.open("/")
    await user.should_see("Formular bei Beispiel GmbH")

    await _press(user, "x")

    assert db.get_job(con, job_id)["status"] == "new", \
        "x threw away the only hint that an application may be out"
    assert db.count_started_forms(con) == 1
    await user.should_see("hast du dich beworben?")


async def test_the_cockpits_old_address_lands_on_the_postings(user: User,
                                                              con, data_dir):
    """The route string being present in the source proves nothing about where
    it goes. An old tab or a bookmark has to land somewhere real."""
    job_id = _posting(con)
    await user.open(f"/cockpit/{job_id}")
    await user.should_see("Stellen")
    await user.should_see("Beispiel GmbH")


async def test_raising_the_daily_limit_frees_the_button_by_itself(
        user: User, con, data_dir):
    """`db.data_signature` reads tables only, by design — so a screen that also
    states a SETTING has to sign it, or the button keeps saying the limit is
    used up after he has raised it in Einstellungen, and after midnight, until
    he reloads. Adding it to the row fingerprint cannot help: the fingerprint
    is only recomputed when a refresh runs, and a refresh only runs when the
    signature moved."""
    job_id = _posting(con)
    db.set_apply_channel(con, job_id, "ats_form", "JOIN", "https://join.com/x/apply")
    db.set_setting(con, "daily_draft_cap", "0")
    con.commit()
    await user.open("/")
    await user.should_see("Tageslimit")

    db.set_setting(con, "daily_draft_cap", "10")   # raised in another tab
    con.commit()
    await _tick(user)

    await user.should_see("Bewerbung starten")
    button = next(e for e in user.client.elements.values()
                  if isinstance(e, ui.button) and "Bewerbung starten" in str(e.text))
    assert button.enabled, "the button is still refusing a limit he has raised"


async def test_an_oversized_mappe_still_says_so_on_the_new_path(
        user: User, con, data_dir, monkeypatch):
    """The portal budget is 2 MB and his real Mappe measures about 2.1. The
    step this press replaced warned about it, and an oversized upload fails in
    front of the employer rather than quietly."""
    from jobdeck.services import drafting, mappe
    job_id = _posting(con)
    db.set_apply_channel(con, job_id, "ats_form", "JOIN", "https://join.com/x/apply")
    con.commit()
    await user.open("/")
    monkeypatch.setattr(ui.navigate, "to", lambda url, **kw: None)

    async def letter(jid):
        with db.db() as c:
            db.upsert_draft(c, jid, {"status": "ready", "anschreiben_body": "X"})
            c.commit()
        return {"ok": True, "error": "", "draft": {"id": 1}}

    async def built(jid):
        return {"ok": True, "error": "", "pdf_path": "/tmp/m.pdf",
                "warning": "Mappe is 2.1 MB — over the 2.0 MB target for this "
                           "channel; the quality floor stops further compression",
                "pages": 10, "size_bytes": 2_100_000,
                "size_before_bytes": 3_700_000, "compression": "", "anlagen": []}

    monkeypatch.setattr(drafting, "draft_for_job", letter)
    monkeypatch.setattr(mappe, "create_mappe", built)
    from jobdeck.ui import helpers
    monkeypatch.setattr(helpers, "open_in_system", lambda path: None)

    user.find("Bewerbung starten", kind=ui.button).click()
    await asyncio.sleep(0.8)

    await user.should_see("over the 2.0 MB target")


async def test_the_undo_timer_survives_the_next_press(user: User, con, data_dir,
                                                      monkeypatch):
    """NiceGUI reads a timer's parent slot BEFORE its own stop check, so a
    one-shot parked in `overlay` — which every handler clears at its top —
    writes an ERROR traceback instead of quietly stopping.

    Reachable in ten seconds by the most ordinary sequence there is: record
    one application, start the next. It is the same failure class as the queue
    timer that logged whenever its page was left."""
    done = _posting(con)
    db.mark_form_opened(con, done)
    nxt = _posting(con, external_id="e2", title="Django Entwickler",
                   company="Zweite GmbH")
    db.set_apply_channel(con, nxt, "ats_form", "JOIN", "https://join.com/x/apply")
    con.commit()
    await user.open("/")
    monkeypatch.setattr(ui.navigate, "to", lambda url, **kw: None)

    user.find("Abgeschickt", kind=ui.button).click()
    await asyncio.sleep(0.4)
    timers = [e for e in user.client.elements.values()
              if isinstance(e, ui.timer) and getattr(e.callback, "__name__", "") == "dismiss"]
    assert len(timers) == 1, "the undo bar has no auto-expiry"

    # the next press. `start_application` clears the overlay before anything.
    user.find("Bewerbung starten", kind=ui.button).click()
    await asyncio.sleep(0.6)

    assert not timers[0].is_deleted, "the one-shot was parked in a cleared slot"
    with user.client:
        await timers[0].callback()      # must not raise


async def test_the_strip_names_the_file_the_upload_dialog_will_ask_for(
        user: User, con, data_dir):
    """"Mappe bereit" is true and useless at the moment the form says "Datei
    auswählen". The file manager the app opens lands BEHIND the employer's tab,
    and the path was reachable only through a menu he had no reason to open —
    so he filled in a form and could not find his own Bewerbungsmappe.

    The name is on the strip now, and one press puts the full path on the
    clipboard: Ctrl+L, Ctrl+V is the way into any file dialog."""
    from jobdeck import config
    job_id = _posting(con)
    db.mark_form_opened(con, job_id)
    staged = f"{config.UPLOAD_DIR}/Bewerbung_Andrei_Sili_Beispiel_GmbH.pdf"
    db.set_upload(con, job_id, staged, "vollständig")
    con.commit()

    await user.open("/")

    await user.should_see("Bewerbung_Andrei_Sili_Beispiel_GmbH.pdf")
    button = next(e for e in user.client.elements.values()
                  if isinstance(e, ui.button)
                  and "Bewerbung_Andrei_Sili_Beispiel_GmbH.pdf" in str(e.text))
    assert button.enabled


async def test_a_posting_with_nothing_staged_offers_no_path(
        user: User, con, data_dir):
    """A copy button for a file that is not there is worse than none."""
    job_id = _posting(con)
    db.mark_form_opened(con, job_id)
    con.commit()

    await user.open("/")

    await user.should_see("Mappe NICHT fertig")
    assert not any(isinstance(e, ui.button) and "⧉" in str(e.text)
                   for e in user.client.elements.values())


async def test_the_held_back_pile_offers_a_way_out_and_it_works(
        user: User, con, data_dir):
    """The pile exists so a posting can still be reached. Before this the only
    thing waiting there was the sentence explaining why it could not be — a
    room with no door, which is the shape a review panel has already caught on
    this screen once.

    Driven in the rendered page rather than asserted on the step model: a step
    that is never rendered, or rendered without a handler, leaves the model
    test green and the button dead."""
    from jobdeck import attempts

    job_id = _posting(con, company="Beispiel GmbH")
    db.add_bewerbung(con, {
        "gesendet_am": (datetime.date.today()
                        - datetime.timedelta(days=5)).isoformat(),
        "firma": "Beispiel GmbH", "email": "", "kanal": "E-Mail",
        "status": "Gesendet"})
    con.commit()
    await user.open("/")
    select = next(iter(user.find(marker="view-select").elements))
    select.set_value("firma_kontaktiert")
    await asyncio.sleep(0.3)

    # the press IS the confirmation: on this screen a button carries its own
    # label and only ⏎ opens a dialog, because ⏎ moves down a list and the
    # label under it changes with the row
    user.find("Trotzdem bewerben", kind=ui.button).click()
    await asyncio.sleep(0.5)

    stored = con.execute(
        "SELECT * FROM application_attempts WHERE job_id=?", (job_id,)
    ).fetchone()
    assert stored is not None, "nothing recorded the candidate's answer"
    assert stored["override_confirmed_at"], "the confirmation left no evidence"
    assert stored["state"] == attempts.RELEASED, (
        "answering the hold must not claim the posting"
    )
    # …and the gate now agrees with the screen
    assert attempts.decide_for_job(con, db.get_job(con, job_id)).allowed is True


async def test_a_permanent_block_offers_no_way_out(user: User, con, data_dir):
    """A second application to the very same position is not the candidate's
    to overrule, so the press must not even be drawn."""
    job_id = _posting(con, company="Beispiel GmbH")
    title = db.get_job(con, job_id)["title"]
    bew = db.add_bewerbung(con, {
        "gesendet_am": (datetime.date.today()
                        - datetime.timedelta(days=400)).isoformat(),
        "firma": "Beispiel GmbH", "email": "", "kanal": "E-Mail",
        "status": "Absage"})
    con.execute(
        "INSERT INTO application_attempts (idempotency_key, state, company,"
        " company_key, position, channel, bewerbung_id, created_at, updated_at)"
        " VALUES ('bewerbung:x', 'recorded', 'Beispiel GmbH', 'beispiel gmbh',"
        " ?, 'E-Mail', ?, '2025-01-01', '2025-01-01')", (title, bew))
    con.commit()

    await user.open("/")
    select = next(iter(user.find(marker="view-select").elements))
    select.set_value("firma_kontaktiert")
    await asyncio.sleep(0.3)

    with pytest.raises(AssertionError):
        user.find("Trotzdem bewerben", kind=ui.button)


async def test_the_strip_offers_every_part_a_portal_asks_for(
        user: User, con, data_dir):
    """A portal form asks for the Lebenslauf, the Anschreiben and the
    Zeugnisse one upload field each. The strip carries one press per file,
    named by what it is — four forty-character file names in one row say
    less than four words, and the press puts the full path on the clipboard
    anyway."""
    from jobdeck import config
    job_id = _posting(con)
    db.mark_form_opened(con, job_id)
    folder = config.UPLOAD_DIR
    db.set_upload(con, job_id, f"{folder}/Bewerbung_A_B.pdf", "vollständig")
    db.set_documents(con, job_id, [
        {"kind": db.DOC_MAPPE, "path": "/x/Bewerbung_A_B.pdf",
         "staged_path": f"{folder}/Bewerbung_A_B.pdf"},
        {"kind": db.DOC_ANSCHREIBEN, "path": "/x/Anschreiben_A_B.pdf",
         "staged_path": f"{folder}/Anschreiben_A_B.pdf"},
        {"kind": db.DOC_LEBENSLAUF, "path": "/x/Lebenslauf_A_B.pdf",
         "staged_path": f"{folder}/Lebenslauf_A_B.pdf"},
        {"kind": db.DOC_ANLAGEN, "path": "/x/Anlagen_A_B.pdf",
         "staged_path": ""},   # taken out of the folder: no press for it
    ])
    con.commit()

    await user.open("/")

    await user.should_see("Mappe bereit · auch einzeln")
    chips = [str(e.text) for e in user.client.elements.values()
             if isinstance(e, ui.button) and str(e.text).startswith("⧉")]
    assert chips == ["⧉ Mappe", "⧉ Anschreiben", "⧉ Lebenslauf"]


async def test_a_part_landing_in_the_background_reaches_the_strip(
        user: User, con, data_dir):
    """The build runs in a worker thread while the page is open: the strip
    has to pick up a part that was staged after the page was drawn, without
    anything else about the posting changing."""
    from jobdeck import config
    job_id = _posting(con)
    db.mark_form_opened(con, job_id)
    folder = config.UPLOAD_DIR
    db.set_upload(con, job_id, f"{folder}/Bewerbung_A_B.pdf", "vollständig")
    db.set_documents(con, job_id, [
        {"kind": db.DOC_MAPPE, "path": "/x/Bewerbung_A_B.pdf",
         "staged_path": f"{folder}/Bewerbung_A_B.pdf"}])
    con.commit()
    await user.open("/")
    await user.should_see("⧉ Mappe")

    db.set_documents(con, job_id, [
        {"kind": db.DOC_MAPPE, "path": "/x/Bewerbung_A_B.pdf",
         "staged_path": f"{folder}/Bewerbung_A_B.pdf"},
        {"kind": db.DOC_LEBENSLAUF, "path": "/x/Lebenslauf_A_B.pdf",
         "staged_path": f"{folder}/Lebenslauf_A_B.pdf"}])
    con.commit()
    await _tick(user)

    await user.should_see("⧉ Lebenslauf")
