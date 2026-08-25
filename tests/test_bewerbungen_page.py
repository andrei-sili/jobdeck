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
def _figures(user: User) -> dict[str, str]:
    """{label: count} for every row of the two figure groups.

    Keyed by label rather than positional, because the element registry does
    not promise document order — what has to be pinned is that each count
    belongs to the name beside it.

    Read as PAIRS, because `should_see` is a substring match over every
    visible element: the first version of the test below looped over
    ("3", "1", "2", "0") and three of those four are satisfied by the date
    "2026-08-11" that `_app_row` writes into the register list underneath.
    Every count could be hardcoded to nought with the suite green — on the
    assertion written to defend the decision that an invitation of nought is
    DRAWN rather than hidden."""
    groups = [e for e in user.find(marker=None).elements
              if "jd-funnel" in getattr(e, "_classes", [])]
    pairs: dict[str, str] = {}
    for group in groups:
        name = ""
        for child in group.default_slot.children:
            classes = getattr(child, "_classes", [])
            if "name" in classes:
                name = str(getattr(child, "text", ""))
            elif "num" in classes:
                pairs[name] = str(getattr(child, "text", ""))
    return pairs


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

    assert _figures(user) == {
        "im Register": "3",
        "noch ohne Antwort": "1",
        "beantwortet": "2",
        "Einladungen": "0",
        "Absagen": "1",
        "sonstige Antworten": "1",
    }


async def test_the_answer_time_sentence_reaches_the_screen(user: User, con):
    """No page test seeded enough measured answers, so `if sentence:` was
    False in all forty of them and the two labels that draw the slice's
    headline statement were never executed. Unit-tested as a string, untested
    as something the screen shows."""
    for n in range(register.ENOUGH_FOR_A_TIME):
        row_id = _app_row(con, firma=f"Antwortende GmbH {n}",
                          gesendet_am="2026-08-01", status="Absage")
        db.add_email_log(con, {
            "direction": "inbound", "gmail_message_id": f"mail{n}",
            "internal_date": "2026-08-05T09:00:00", "bewerbung_id": row_id,
            "classification": "absage",
        })
    con.commit()

    await user.open("/bewerbungen")

    await user.should_see("Im Median kam eine Antwort nach 4 Tagen.")
    await user.should_see(f"Gemessen an {register.ENOUGH_FOR_A_TIME} der "
                          f"{register.ENOUGH_FOR_A_TIME} beantworteten "
                          f"Bewerbungen")


async def test_a_card_with_too_few_answers_says_nothing_about_timing(
        user: User, con):
    """The other half of the same branch, and the one that matters: a median
    over three replies is a coincidence, and this screen may not print a
    figure it cannot stand behind."""
    _app_row(con, firma="Eine GmbH", status="Absage")

    await user.open("/bewerbungen")

    drawn = [str(getattr(e, "text", "")) for e in user.find(marker=None).elements]
    assert not [t for t in drawn if t.startswith("Im Median")]
    assert not [t for t in drawn if t.startswith("Gemessen an")]


async def test_only_the_whole_is_drawn_solid(user: User, con):
    """A solid bar means "this is the figure the others are shares of".

    The answers group is three PARTS of the line above it, and drawing the
    invitation solid put the only solid bar on the only value that is nought
    — invisible by construction — while the two real numbers were dimmed as
    though they were the aside. Found by opening the page, not by a test:
    both groups' arithmetic was already right."""
    _app_row(con, firma="Eine GmbH")
    _app_row(con, firma="Andere GmbH", status="Absage")

    await user.open("/bewerbungen")

    # Scoped to the two groups of figures. The comparison panels further down
    # draw bars too, and theirs mean something else entirely — a share of a
    # channel's own population, with no whole among them to be solid.
    groups = [e for e in user.find(marker=None).elements
              if "jd-funnel" in getattr(e, "_classes", [])]
    assert len(groups) == 2, "the register and what came back"
    solid = []
    for group in groups:
        name = ""
        for child in group.default_slot.children:
            classes = getattr(child, "_classes", [])
            if "name" in classes:
                name = str(getattr(child, "text", ""))
            elif "jd-bar" in classes:
                solid += [name for inner in child.default_slot.children
                          if "dim" not in getattr(inner, "_classes", [])]
    # Named, not counted. Counting alone could not see the solid bar sitting
    # on the wrong row, which is the property this test's own name states.
    assert solid == ["im Register"]


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


# --------------------------------------------------------------------------
# The order the list is in — named, kept, and never a second rule
# --------------------------------------------------------------------------
def _marked(user: User, marker: str) -> list:
    with user.client:
        return [el for el in user.client.elements.values()
                if marker in getattr(el, "_markers", [])]


def _listed(user: User) -> list[int]:
    """The application ids in the order the register drew them.

    `user.find()` returns a SET, so it cannot answer a question about order.
    `client.elements` is keyed by element id, which NiceGUI hands out in
    creation order, and the rows are created in list order.
    """
    with user.client:
        return [int(marker.split("-")[1])
                for el in user.client.elements.values()
                for marker in getattr(el, "_markers", [])
                if marker.startswith("application-")]


async def test_the_control_offers_exactly_the_orders_that_exist(user: User,
                                                                con):
    """Read off the control rather than off the page: an order drawn with no
    rule behind it silently lists the default, and the control then names an
    order the screen is not in."""
    await user.open("/bewerbungen")
    await user.should_see("Sortierung")

    assert _marked(user, "register-sort")[0].options == register.SORT_LABELS


async def test_the_longest_silence_can_be_brought_to_the_top(user: User, con):
    """The reason this exists. The list orders by the SEND date and the
    "Wartet seit" cell counts from the last contact, so on his real register
    the longest-silent application sits at row 99 of 141 — and the silence
    panel names only the first eight of eighty-five.
    """
    fresh = _app_row(con, firma="Frisch GmbH", gesendet_am="2026-08-20")
    old = _app_row(con, firma="Alt GmbH", gesendet_am="2026-05-01")
    await user.open("/bewerbungen")
    assert _listed(user) == [fresh, old]

    _marked(user, "register-sort")[0].set_value("waiting")
    await asyncio.sleep(0.3)

    assert _listed(user) == [old, fresh]


async def test_an_answered_application_never_leads_an_order_about_waiting(
        user: User, con):
    answered = _app_row(con, firma="Beantwortet GmbH",
                        gesendet_am="2026-01-01", status="Absage")
    waiting = _app_row(con, firma="Wartet GmbH", gesendet_am="2026-08-20")
    await user.open("/bewerbungen")

    _marked(user, "register-sort")[0].set_value("waiting")
    await asyncio.sleep(0.3)

    assert _listed(user) == [waiting, answered]


async def test_the_alphabet_is_an_alphabet_and_not_a_code_point_order(
        user: User, con):
    """An umlaut belongs under its base letter, and a lower-case name under
    its own. `fold` and `norm` both leave the umlaut in place, which files it
    past 'z' — after every other company on the screen.
    """
    zeta = _app_row(con, firma="Zeta GmbH", gesendet_am="2026-08-20")
    uber = _app_row(con, firma="Übersicht GmbH", gesendet_am="2026-08-19")
    alpha = _app_row(con, firma="alpha GmbH", gesendet_am="2026-08-18")
    await user.open("/bewerbungen")

    _marked(user, "register-sort")[0].set_value("firma")
    await asyncio.sleep(0.3)

    assert _listed(user) == [alpha, uber, zeta]


async def test_the_order_is_where_he_left_it_next_visit(user: User, con):
    """A control he has to set again every time is one nobody uses twice."""
    fresh = _app_row(con, firma="Frisch GmbH", gesendet_am="2026-08-20")
    old = _app_row(con, firma="Alt GmbH", gesendet_am="2026-05-01")
    db.set_setting(con, register.SORT_SETTING, "waiting")
    con.commit()

    await user.open("/bewerbungen")

    assert _listed(user) == [old, fresh]
    assert _marked(user, "register-sort")[0].value == "waiting"


async def test_choosing_an_order_records_it(user: User, con):
    _app_row(con, firma="Eine GmbH")
    await user.open("/bewerbungen")

    _marked(user, "register-sort")[0].set_value("firma")
    await asyncio.sleep(0.3)

    assert db.get_setting(con, register.SORT_SETTING, "") == "firma"


async def test_an_order_stored_as_nonsense_opens_the_screen_anyway(
        user: User, con):
    """The value reaches the page from a settings row, so a hand-edited or
    half-migrated one must degrade to the order the screen was built around."""
    _app_row(con, firma="Eine GmbH")
    db.set_setting(con, register.SORT_SETTING, "nach Lust und Laune")
    con.commit()

    await user.open("/bewerbungen")

    await user.should_see("Eine GmbH")
    assert _marked(user, "register-sort")[0].value == register.DEFAULT_SORT


def test_the_screen_watches_the_order_a_second_tab_could_change(con):
    """No table moves when a setting does, so without this the control keeps
    naming the order this tab chose after another tab has changed it."""
    before = register.signature(con)
    db.set_setting(con, register.SORT_SETTING, "waiting")
    con.commit()

    assert register.signature(con) != before


async def _tick(user: User) -> None:
    """Fire every live timer on the page by hand — the interval is half a
    minute. Every one, not the first: this screen carries its own watcher and
    the rail's. Inside the client context, which is what NiceGUI's own timer
    loop enters before each invocation."""
    from nicegui import ui as _ui
    timers = [e for e in list(user.client.elements.values())
              if isinstance(e, _ui.timer)]
    assert timers, "the page has no live timer at all"
    with user.client:
        for timer in timers:
            await timer.callback()
    await asyncio.sleep(0.1)


async def test_an_order_chosen_in_another_tab_reaches_this_one(user: User, con):
    """The setting is watched precisely so this happens. Without the write
    back into the select, the list would reorder under a control still naming
    the order this tab chose — the shape of a control that lies.
    """
    fresh = _app_row(con, firma="Frisch GmbH", gesendet_am="2026-08-20")
    old_one = _app_row(con, firma="Alt GmbH", gesendet_am="2026-05-01")
    await user.open("/bewerbungen")
    assert _listed(user) == [fresh, old_one]

    db.set_setting(con, register.SORT_SETTING, "waiting")   # the other tab
    con.commit()
    await _tick(user)

    assert _listed(user) == [old_one, fresh]
    assert _marked(user, "register-sort")[0].value == "waiting"


async def test_the_write_back_into_the_select_does_not_store_what_it_just_read(
        user: User, con):
    """`refresh` sets the select's value, and NiceGUI fires a change handler on
    a server-side write whenever the value actually differs — which is exactly
    the case this path creates. Without the early return the handler stores the
    value it was just handed and redraws the list, on every tick that carries
    another tab's choice.

    The earlier version of this test set the select to the value it already
    held; NiceGUI's own BindableProperty returns before the handler in that
    case, so it passed with the guard deleted.
    """
    _app_row(con, firma="Eine GmbH")
    await user.open("/bewerbungen")
    db.set_setting(con, register.SORT_SETTING, "firma")     # the other tab
    con.commit()

    writes = []
    original = bewerbungen._store_sort
    bewerbungen._store_sort = lambda value: writes.append(value)
    try:
        await _tick(user)
    finally:
        bewerbungen._store_sort = original

    assert _marked(user, "register-sort")[0].value == "firma"
    assert writes == [], "the refresh stored the order it had just read"


async def test_a_status_view_with_nothing_waiting_says_why_the_order_is_flat(
        user: User, con):
    """Three of the six status views hold no application that is still
    waiting, and on the real ledger they are 56 rows of 141. Under those the
    list is in a well-defined order that separates nothing, which reads as a
    control that has stopped working."""
    _app_row(con, firma="Abgelehnt GmbH", status="Absage")
    _app_row(con, firma="Wartet GmbH", status="Gesendet")
    db.set_setting(con, register.SORT_SETTING, "waiting")
    con.commit()
    await user.open("/bewerbungen")
    await user.should_not_see("Keine davon wartet noch")

    _marked(user, "register-status")[0].set_value("Absage")
    await asyncio.sleep(0.3)

    await user.should_see("Keine davon wartet noch")


async def test_the_note_is_drawn_above_the_column_heads_not_among_the_rows(
        user: User, con):
    """Drawn after the heads it sits exactly where a row sits and reads as
    one — which is how it looked the first time the page was opened. Found by
    opening it, so it is pinned by position rather than by presence."""
    _app_row(con, firma="Abgelehnt GmbH", status="Absage")
    db.set_setting(con, register.SORT_SETTING, "waiting")
    con.commit()
    await user.open("/bewerbungen")

    with user.client:
        order = [(el.id, str(getattr(el, "text", "")))
                 for el in user.client.elements.values()]
    note = next(i for i, t in order if t.startswith("Keine davon wartet"))
    head = next(i for i, t in order if t == "Firma")
    first_row = next(i for i, t in order if t == "Abgelehnt GmbH")

    assert note < head < first_row
