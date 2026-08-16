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

    await user.should_see("Der Trichter")
    await user.should_see("Das Register")
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

    await user.should_see("TESTMODUS")
    await user.should_see("Nichts wartet")


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
