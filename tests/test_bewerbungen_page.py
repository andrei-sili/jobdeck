"""The register screen's contract: what it draws, and what it refuses to trust.

The screen it replaces handed Quasar's `rowClick` payload — a dict the BROWSER
composed — straight into the dialog, so the path later given to `xdg-open` and
the id later deleted both came from the client. That is the property most worth
pinning here, because nothing about the page LOOKS different when it breaks.
"""

import ast
import asyncio
import pathlib
import sys

import pytest
from nicegui.testing import User

from jobdeck import db
from jobdeck.services import register
from jobdeck.ui.pages import bewerbungen

pytest_plugins = ["nicegui.testing.user_plugin"]

pytestmark = [pytest.mark.nicegui_main_file("tests/nicegui_main.py"),
              pytest.mark.usefixtures("data_dir")]


@pytest.fixture(autouse=True)
def _keep_the_package_importable():
    """See test_draft_visibility_pages.py: NiceGUI's teardown pops the page
    module AND its parents out of sys.modules."""
    saved = {name: mod for name, mod in sys.modules.items()
             if name == "jobdeck" or name.startswith("jobdeck.")}
    yield
    sys.modules.update(saved)


def _app_row(con, firma="Beispiel GmbH", **over):
    values = {"gesendet_am": "2026-08-11", "firma": firma,
              "email": "hr@beispiel.example", "kanal": "E-Mail",
              "status": "Gesendet"}
    values.update(over)
    row_id = db.add_bewerbung(con, values)
    con.commit()
    return row_id


# --------------------------------------------------------------------------
# The browser never says which row a click was on
# --------------------------------------------------------------------------
def test_no_row_takes_its_identity_from_the_client():
    """The old screen registered `table.on("rowClick", …)` with no argument
    filter and used the client-supplied dict as-is: a forged websocket event
    could hand an arbitrary path to `open_in_system` (xdg-open) or a foreign
    id to the delete path. Every row here closes over its id in Python.

    Written as a rule over the source rather than as one assertion about one
    handler, because the defect is a SHAPE — the next table added to this page
    would reintroduce it and no behavioural test would notice.
    """
    source = pathlib.Path(bewerbungen.__file__).read_text()
    offenders = _client_payload_reads(source)

    assert offenders == [], (
        "a client-composed row payload is back on this page: " +
        ", ".join(offenders))


def _client_payload_reads(source: str) -> list[str]:
    """Every place this module reads data OUT of a browser-sent event.

    The shape, not two spellings of it. The first version of this rule listed
    the event names "rowClick"/"row-click"/"selection" and the constructors
    `ui.table`/`ui.aggrid` — so `element.on("click", lambda e:
    open_in_system(e.args["dokument"]))` sailed through it, which is the
    original defect written on one line.

    What actually matters is that no handler on this page ever SUBSCRIPTS or
    attribute-reads an event's payload: `e.args` is whatever the browser sent.
    A handler may take the event and ignore it, which is what every row does.
    """
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != "args":
            continue
        # `<something>.args` — flag it unless it is plainly not an event, and
        # err toward flagging: a false positive here costs one rewritten line.
        offenders.append(f"line {node.lineno}: {ast.unparse(node)}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) in (
                "ui.table", "ui.aggrid", "ui.table.from_pandas",
                "ui.aggrid.from_pandas"):
            offenders.append(f"line {node.lineno}: {ast.unparse(node.func)}")
    return offenders


def test_the_rule_catches_the_defect_written_any_other_way():
    """A rule that only knows the spelling it was written against is a rule
    that passes the next spelling. These four are the ways the deleted screen's
    defect can come back; all four must be refused."""
    reintroductions = [
        'table.on("rowClick", lambda e: edit(e.args[1]))',
        'element.on("click", lambda e: open_in_system(e.args["dokument"]))',
        'grid = ui.aggrid.from_pandas(df)',
        'def h(e):\n    path = e.args[1]["dokument"]\n    open_in_system(path)',
    ]
    for source in reintroductions:
        assert _client_payload_reads(source), f"not caught: {source!r}"

    # …and the shape the page really uses is not flagged
    assert _client_payload_reads(
        'element.on("click", lambda _=None, i=row_id: open_application(i))') == []


async def test_opening_a_row_reads_it_from_the_database(user: User, con):
    """The row the dialog shows is the row the server read, so what the dialog
    later hands to xdg-open cannot have been chosen by the browser."""
    row_id = _app_row(con, firma="Server GmbH")
    con.execute("UPDATE bewerbungen SET ansprechpartner=? WHERE id=?",
                ("Frau Beispiel", row_id))
    con.commit()

    await user.open("/bewerbungen")
    user.find(marker=f"application-{row_id}").click()
    await asyncio.sleep(0.3)

    await user.should_see("Frau Beispiel")


async def test_a_row_deleted_in_another_tab_says_so_instead_of_opening(
        user: User, con):
    """The click carries an id, and by the time it is read the row may be
    gone: this page refreshes itself, so the list he clicked can be seconds
    old. `_row` answering None must not become a dialog full of blanks."""
    row_id = _app_row(con, firma="Verschwunden GmbH")
    await user.open("/bewerbungen")
    db.delete_bewerbung(con, row_id)
    con.commit()

    user.find(marker=f"application-{row_id}").click()
    await asyncio.sleep(0.3)

    await user.should_see("gibt es nicht mehr")


# --------------------------------------------------------------------------
# What the screen states
# --------------------------------------------------------------------------
async def test_the_screen_states_the_shape_before_it_lists_the_rows(
        user: User, con):
    _app_row(con, firma="Eine GmbH")
    _app_row(con, firma="Andere GmbH", status="Absage")

    await user.open("/bewerbungen")

    await user.should_see("Das Register")
    await user.should_see("Was zurückkam")
    await user.should_see("Rhythmus")
    await user.should_see("Wer schweigt, seit wann")
    await user.should_see("Was antwortet")
    await user.should_see("Die Bewerbungen")
    await user.should_see("Eine GmbH")


async def test_the_letter_that_went_out_is_shown_with_its_application(
        user: User, con):
    """The register's own answer to "what did this company actually read".
    Only a letter this application carried — 'filed' or 'sent'."""
    row_id = _app_row(con, firma="Formular GmbH", kanal="Online-Portal")
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "x", "company": "Formular GmbH",
        "title": "Entwickler", "url": "https://x.example/1"})
    draft_id = db.upsert_draft(con, job_id, {
        "status": "ready", "betreff": "Bewerbung als Entwickler",
        "anschreiben_body": "Sehr geehrte Damen und Herren,"})
    db.file_draft(con, draft_id, row_id)
    con.commit()

    await user.open("/bewerbungen")
    user.find(marker=f"application-{row_id}").click()
    await asyncio.sleep(0.3)

    await user.should_see("Das Anschreiben, das rausging")


def test_a_draft_that_never_went_with_this_application_is_not_offered(con):
    """A draft merely sitting on the same posting is not what the employer
    read — only a letter bound to THIS application counts."""
    row_id = _app_row(con, firma="Offen GmbH")
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "x", "company": "Offen GmbH",
        "title": "Entwickler", "url": "https://x.example/1"})
    db.upsert_draft(con, job_id, {"status": "ready", "betreff": "Entwurf"})
    con.commit()

    assert bewerbungen._letter(row_id) is None


# --------------------------------------------------------------------------
# Wording that must not drift from what the data can support
# --------------------------------------------------------------------------
def test_a_waiting_application_is_amber_and_a_closed_one_is_not():
    """Amber means "something is waiting" everywhere in this app. A rejection
    is a closed question and must not wear the colour of an open one."""
    assert bewerbungen._pill_class("Gesendet") == "warn"
    assert bewerbungen._pill_class("In Bearbeitung") == "warn"
    assert bewerbungen._pill_class("Absage") == ""
    assert bewerbungen._pill_class("Einladung") == "ok"


def test_an_unknown_status_is_never_dressed_as_an_answer():
    """`STATUS_OPTIONS` can grow. A value this map has not been taught must
    fall back to neutral rather than inherit whichever branch came last."""
    assert bewerbungen._pill_class("Irgendetwas Neues") == ""


def test_the_follow_up_threshold_survives_whatever_is_stored():
    """It is free text in a table he can edit, and it is read while the page
    is being built — `int("")` here would take down the whole screen, which is
    the shape that once took down the inbox over an age threshold."""
    assert register.follow_up_setting("21") == 21
    for stored in ("", "   ", "bald", None, "inf", "-3", "0"):
        assert register.follow_up_setting(stored) == register.FOLLOW_UP_DEFAULT


# --------------------------------------------------------------------------
# One rubric, two faces
# --------------------------------------------------------------------------
async def test_each_face_of_the_rubric_can_reach_the_other(user: User, con):
    """The stack that drains and the register that grows are two routes — a
    bookmark keeps working and the send path was not relocated for a layout —
    so the strip is what makes them read as one rubric rather than two."""
    await user.open("/bewerbungen")
    await user.should_see("Postausgang")

    await user.open("/queue")
    await user.should_see("Register")
    await user.should_see("Postausgang")


async def test_the_send_screen_speaks_the_language_of_the_app(user: User, con):
    """It sat in English long after the rubric around it changed language, and
    it is the last screen a message passes through."""
    await user.open("/queue")

    await user.should_see("Testmodus")
    await user.should_see("Nichts offen")


# --------------------------------------------------------------------------
# A generic navigator carries its own gate
# --------------------------------------------------------------------------
def test_a_tab_may_only_open_a_route_of_this_app():
    """`ui.navigate.to` is window.open in the app's own origin. The AST rule
    that guards that refuses a call which SUBSCRIPTS its way to a target —
    which this helper does not do, because its target arrives as a plain
    parameter. So the helper gates itself, or the next caller hands it a
    stored employer URL and nothing notices."""
    from jobdeck.ui import layout
    for hostile in ("javascript:alert(1)", "//evil.example/x", "/\\evil.example",
                    "https://evil.example/x", "data:text/html,x", ""):
        with pytest.raises(ValueError):
            layout.tabs("a", [("a", "A", "/ok"), ("b", "B", hostile)])


def test_the_rubrics_own_tabs_are_in_app_routes():
    from jobdeck.ui import layout
    assert all(layout._is_internal(path)
               for _key, _label, path in layout.BEWERBUNGEN_TABS)


# --------------------------------------------------------------------------
# Sentences that were true only of the shape they were written for
# --------------------------------------------------------------------------
async def test_an_empty_register_is_not_congratulated(user: User, con):
    """"Jede Bewerbung ist beantwortet" is also what an EMPTY register
    produces — and what one holding only withdrawn applications produces."""
    await user.open("/bewerbungen")

    await user.should_see("Noch keine Bewerbung im Register.")


async def test_a_quiet_window_is_not_called_a_stretch_without_pauses(
        user: User, con):
    """"ohne Pause" is only true of a window that HAS working days to be
    uninterrupted between. Over sixty empty columns it praised a stretch in
    which nothing at all went out."""
    await user.open("/bewerbungen")

    await user.should_see("nichts raus")


async def test_a_date_no_panel_could_read_is_refused_at_the_dialog(
        user: User, con):
    """It printed in the register and was read as no date at all by every
    panel above it: the age column said "?", the silence panel filed it under
    "seit unbekannt", and the rhythm strip moved no column for it."""
    row_id = _app_row(con, firma="Datum GmbH")
    await user.open("/bewerbungen")
    user.find(marker=f"application-{row_id}").click()
    await asyncio.sleep(0.3)

    user.find("Gesendet am (JJJJ-MM-TT)").type("15.08.2026")
    user.find("Speichern").click()
    await asyncio.sleep(0.3)

    await user.should_see("JJJJ-MM-TT")
    assert db.get_bewerbung(con, row_id)["gesendet_am"] == "2026-08-11"


def test_a_letter_is_offered_only_in_a_state_that_means_it_went_out(con):
    """The query had no status predicate, so the guarantee rested entirely on
    a convention about which writers touch `bewerbung_id`."""
    row_id = _app_row(con, firma="Offen GmbH")
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "x", "company": "Offen GmbH",
        "title": "Entwickler", "url": "https://x.example/1"})
    draft_id = db.upsert_draft(con, job_id, {"status": "ready",
                                             "betreff": "Entwurf"})
    con.execute("UPDATE drafts SET bewerbung_id=? WHERE id=?",
                (row_id, draft_id))
    con.commit()

    assert bewerbungen._letter(row_id) is None


async def test_opening_a_dateless_application_does_not_stamp_it_with_today(
        user: User, con):
    """An imported row can state no date at all, and the panel above shows it
    as "seit unbekannt" on purpose. Pre-filling the field meant that merely
    opening the row and pressing Speichern replaced that with today."""
    row_id = _app_row(con, firma="Ohne Datum GmbH", gesendet_am="")
    await user.open("/bewerbungen")
    user.find(marker=f"application-{row_id}").click()
    await asyncio.sleep(0.3)

    user.find("Speichern").click()
    await asyncio.sleep(0.3)

    assert db.get_bewerbung(con, row_id)["gesendet_am"] == ""


def test_the_screen_watches_the_setting_it_prints_and_colours_by(con):
    """The silence panel states the threshold, sorts by it and colours by it,
    so raising it in Einstellungen has to reach this screen — otherwise the
    number beside "Ab N Tagen" and the rows beneath it describe two different
    settings until the page is reloaded."""
    before = register.signature(con)
    db.set_setting(con, "follow_up_days", "30")
    con.commit()

    assert register.signature(con) != before


# --------------------------------------------------------------------------
# The panel's anti-noise guard, and the figures it guards
# --------------------------------------------------------------------------
def test_two_level_rates_are_reported_as_level():
    """His real register: 27 % against 26 %, which is one application's worth.
    The threshold could be raised to 0.99 — calling every comparison level —
    and the ranking could be INVERTED, both with the suite green."""
    shares = [register.Share("Online-Portal", 11, 41, 11 / 41),
              register.Share("E-Mail", 9, 35, 9 / 35)]

    said = bewerbungen._channel_verdict(shares, True)

    assert "gleichauf" in said
    assert "Online-Portal" in said and "E-Mail" in said
    assert "Danach solltest du dich nicht richten" in said


def test_a_real_difference_names_the_channel_that_answers_more():
    shares = [register.Share("E-Mail", 2, 40, 0.05),
              register.Share("Online-Portal", 20, 40, 0.5)]

    said = bewerbungen._channel_verdict(shares, True)

    assert said.startswith("Online-Portal antwortet häufiger")
    assert "50 %" in said and "5 %" in said


def test_a_population_too_small_for_a_rate_says_so_and_names_the_smallest():
    shares = [register.Share("E-Mail", 2, 40, 0.05),
              register.Share("Post", 1, 3, 1 / 3)]

    said = bewerbungen._channel_verdict(shares, False)

    assert "Bei nur 3 Bewerbungen" in said
    assert "nur die Zahlen" in said


def test_a_panel_with_no_rows_says_nothing_at_all():
    """The sentence pointed at figures that were not drawn."""
    assert bewerbungen._channel_verdict([], False) == ""


# --------------------------------------------------------------------------
# What the panels SAY, not merely that they are there
# --------------------------------------------------------------------------
async def test_the_numbers_print_the_populations_they_measured(user: User, con):
    """The panel that replaced the funnel, held to the same standard: without
    the figures asserted, every label could be relabelled 'x' and every count
    zeroed with the suite green.

    An invitation nobody has received is drawn, not hidden. That is the one
    number on this screen he is working toward, and a scoreboard that omits
    the score until it is non-zero is a scoreboard that never says nought."""
    _app_row(con, firma="Wartende GmbH")
    _app_row(con, firma="Absagende GmbH", status="Absage")
    _app_row(con, firma="Antwortende GmbH", status="Antwort erhalten")

    await user.open("/bewerbungen")

    await user.should_see("Das Register")
    await user.should_see("noch ohne Antwort")
    await user.should_see("Einladungen")
    await user.should_see("Absagen")
    await user.should_see("sonstige Antworten")
    # The counts themselves: 3 in the register, 1 still waiting, 2 answered,
    # and the two answers split one and one with no invitation among them.
    for figure in ("3", "1", "2", "0"):
        await user.should_see(figure)


async def test_the_register_block_names_what_this_app_did_not_do(user: User,
                                                                 con):
    _app_row(con, firma="Von Hand GmbH")

    await user.open("/bewerbungen")

    await user.should_see("im Register")
    await user.should_see("0 über JobDeck · 1 von Hand oder aus der alten Liste")


async def test_the_silence_panel_names_the_company_and_its_age(user: User, con):
    _app_row(con, firma="Schweigt GmbH", gesendet_am="2026-01-01")

    await user.open("/bewerbungen")

    await user.should_see("Schweigt GmbH")
    await user.should_see("Bewerbung ohne Antwort")
    await user.should_see("die am längsten wartende zuerst")


# --------------------------------------------------------------------------
# Controls no test was driving
# --------------------------------------------------------------------------
async def test_the_search_box_narrows_the_register(user: User, con):
    """The LIST narrows; the panels above it do not, because they describe the
    whole register and a filtered funnel would be a different claim. So the
    assertion is the printed range, not the absence of a name — the silent
    company is still named one card higher, on purpose."""
    _app_row(con, firma="Gesucht GmbH")
    _app_row(con, firma="Andere GmbH", email="hr@andere.example")
    await user.open("/bewerbungen")
    await user.should_see("Die Bewerbungen")

    user.find(marker="register-search").type("Gesucht")
    await asyncio.sleep(0.6)

    await user.should_see("1 von 2")
    await user.should_not_see("hr@andere.example")


async def test_a_second_application_at_one_company_is_refused_in_the_dialog(
        user: User, con):
    """The dialog reflects the current company-wide send gate.

    The narrower accepted identity policy in ADR 0002 is not implemented yet.
    """
    _app_row(con, firma="Einmal GmbH")
    await user.open("/bewerbungen")

    user.find("Neue Bewerbung").click()
    await asyncio.sleep(0.3)
    user.find(marker="field-firma").type("Einmal GmbH")
    user.find("Speichern").click()
    await asyncio.sleep(0.4)

    await user.should_see("eine Bewerbung pro Firma")
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 1


async def test_an_application_without_a_company_is_refused(user: User, con):
    await user.open("/bewerbungen")

    user.find("Neue Bewerbung").click()
    await asyncio.sleep(0.3)
    user.find("Speichern").click()
    await asyncio.sleep(0.3)

    await user.should_see("Ohne Firma geht es nicht")
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 0


# --------------------------------------------------------------------------
# The shelf and the tabs: rendered, and pointing where they promise
# --------------------------------------------------------------------------
async def test_the_shelf_is_drawn_and_opens_the_stack_it_describes(user: User,
                                                                   con):
    """It was exercised as a pure string function only: the block that builds
    the button and attaches the navigation could be wrapped in `if False:`
    with the suite green, and its data source could be swapped for an
    unrelated counter."""
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "x", "company": "Firma",
        "title": "Entwickler", "url": "https://x.example/1"})
    db.upsert_draft(con, job_id, {"status": "ready"})
    con.commit()

    await user.open("/bewerbungen")
    await user.should_see("1 Brief wartet")

    user.find(marker="postausgang-shelf").click()
    await asyncio.sleep(0.5)

    await user.should_see("Testmodus")


async def test_an_empty_stack_draws_no_shelf(user: User, con):
    await user.open("/bewerbungen")

    await user.should_not_see("Briefe warten")
    await user.should_not_see("Brief wartet")


async def test_a_tab_actually_navigates(user: User, con):
    """No test clicked one: deleting the click handler left the suite green,
    and both destinations could be pointed at /settings."""
    await user.open("/queue")
    await user.should_see("Nichts offen")

    user.find(marker="tab-register").click()
    await asyncio.sleep(0.5)

    await user.should_see("Das Register")


def test_each_face_of_the_rubric_points_at_its_own_screen():
    from jobdeck.ui import layout
    from jobdeck.ui.pages import queue as queue_page
    paths = {key: path for key, _label, path in layout.BEWERBUNGEN_TABS}
    assert paths["register"] == bewerbungen.BEWERBUNGEN_PATH
    assert paths["postausgang"] == "/queue"
    assert queue_page.queue_page.__module__.endswith("queue")


async def test_the_shelf_is_absent_on_the_page_it_opens(user: User, con):
    """Everything else in the foot reports; the shelf is the one element that
    means "there is something ELSEWHERE to do", and on the Postausgang it was
    a button that navigated to the page you were already on."""
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "x", "company": "Firma",
        "title": "Entwickler", "url": "https://x.example/1"})
    db.upsert_draft(con, job_id, {"status": "ready"})
    con.commit()

    await user.open("/bewerbungen")
    await user.should_see("1 Brief wartet")

    await user.open("/queue")
    await user.should_not_see("1 Brief wartet")


def test_a_bar_never_publishes_a_rate_the_card_refuses_to_state(con):
    """When `enough_for_a_rate` is False the card prints counts and a sentence
    saying a percentage would be noise — and then drew exactly those
    percentages, so one application on a third channel got a full bar."""
    thin = [register.Share("E-Mail", 9, 35, 9 / 35),
            register.Share("Post", 1, 1, 1.0)]
    assert not register.enough_for_a_rate(thin)

    widths = register.bar_widths(thin, "whole")

    assert widths[0] == 1.0, "the population leads when the rate may not"
    assert widths[1] < 0.1


def test_a_tab_target_is_judged_the_way_the_browser_will_read_it():
    """The browser REMOVES ASCII tab, LF and CR before parsing, so
    "/\\t/evil.example/x" reaches window.open as "//evil.example/x" — off this
    origin. A screen must judge a value the way its CONSUMER will."""
    from jobdeck.ui import layout
    for hostile in ("/\t/evil.example/x", "/\n/evil.example/x",
                    "/\r/evil.example/x", "/\t\\evil.example"):
        assert not layout._is_internal(hostile), repr(hostile)
    assert layout._is_internal("/bewerbungen")


def test_deleting_an_application_gives_its_posting_back(con):
    """Clearing the link while leaving `status='applied'` hid the posting from
    every working view with no ledger row behind it and no way back."""
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "x", "company": "Firma GmbH",
        "title": "Entwickler", "url": "https://x.example/1"})
    bewerbung_id = db.apply_job(con, job_id, kanal="E-Mail")
    con.commit()
    assert db.get_job(con, job_id)["status"] == "applied"

    db.delete_bewerbung(con, bewerbung_id)
    con.commit()

    job = db.get_job(con, job_id)
    assert job["status"] == "new"
    assert job["bewerbung_id"] is None


# --------------------------------------------------------------------------
# The Antwort column (Phase 3)
# --------------------------------------------------------------------------
async def test_the_register_shows_when_an_answer_first_arrived(user: User, con):
    """The column derives from status_history — the recording moment for
    imported rows, the ingested transition for read ones — so both kinds of
    answer carry the same kind of date under one column head."""
    answered = _app_row(con, firma="Antwortet GmbH")
    db.set_status(con, answered, "Absage", source="reply_auto", force=False,
                  note="test")
    _app_row(con, firma="Schweigt GmbH", email="hr2@beispiel.example")
    con.commit()
    first_at = db.first_answer_dates(con)[answered][:10]

    await user.open("/bewerbungen")
    await user.should_see("Antwort")
    await user.should_see(first_at)
