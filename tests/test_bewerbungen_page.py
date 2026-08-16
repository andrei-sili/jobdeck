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
    tree = ast.parse(pathlib.Path(bewerbungen.__file__).read_text())

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = ast.unparse(node.func)
        if target.endswith(".on") and node.args:
            event = ast.unparse(node.args[0]).strip("'\"")
            if event in ("rowClick", "row-click", "selection"):
                offenders.append(f"line {node.lineno}: {event}")
        # `e.args` is the client's payload; only its presence in a handler
        # that then reads fields off it is the defect, and the simplest
        # enforceable rule is that this page never unpacks one at all.
        if target in ("ui.table", "ui.aggrid"):
            offenders.append(f"line {node.lineno}: {target}")
    assert offenders == [], (
        "a client-composed row payload is back on this page: " +
        ", ".join(offenders))


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
    await user.should_see("Die Firmen")
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
