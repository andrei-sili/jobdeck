"""The Antworten screen's contract: honesty about the reader, hostile mail
stays text, and the review actions really settle rows.

Reply subjects and bodies are text ANYONE can send. The property most worth
pinning is therefore structural: this module may never hand mail content to
a markup sink — labels only — because nothing about the page looks different
when that breaks.
"""

import ast
import pathlib
import sys

import pytest
from nicegui.testing import User

from jobdeck import config, db, gmail
from jobdeck.ui.pages import antworten

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


def _application(con, firma="Beispiel GmbH") -> int:
    row_id = db.add_bewerbung(con, {
        "gesendet_am": "2026-08-11", "firma": firma,
        "email": "hr@beispiel.example", "kanal": "E-Mail",
        "status": "Gesendet"})
    con.commit()
    return row_id


def _inbound(con, message_id, *, bewerbung_id=None, needs_review=1,
             classification="", classified_by="", subject="Ihre Bewerbung",
             body="", matched_by="thread") -> int:
    row_id = db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": message_id,
        "bewerbung_id": bewerbung_id, "needs_review": needs_review,
        "classification": classification, "classified_by": classified_by,
        "subject": subject, "body_text": body, "snippet": body[:100],
        "matched_by": matched_by, "internal_date": "2026-08-16T14:00:00"})
    con.commit()
    return row_id


# --------------------------------------------------------------------------
# honesty about the reader
# --------------------------------------------------------------------------
async def test_without_a_connection_the_page_says_so(user: User, con):
    await user.open("/antworten")
    await user.should_see("Gmail ist nicht verbunden")


async def test_a_send_only_token_names_what_a_reconnect_adds(
        user: User, con, data_dir):
    config.TOKEN_PATH.write_text("{}", encoding="utf-8")  # exists, no scopes
    await user.open("/antworten")
    await user.should_see("noch nicht lesen")
    await user.should_see("gmail.modify")


async def test_the_page_states_what_is_automatic(user: User, con,
                                                 monkeypatch, data_dir):
    """The tiering decision supersedes the mockup's blanket sentence — the
    screen must say which verdicts file themselves."""
    config.TOKEN_PATH.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gmail, "can_read", lambda: True)
    await user.open("/antworten")
    await user.should_see("trägt JobDeck selbst ein")


# --------------------------------------------------------------------------
# the piles
# --------------------------------------------------------------------------
async def test_a_pending_reply_is_offered_with_all_four_verdicts(
        user: User, con):
    bewerbung_id = _application(con)
    _inbound(con, "m-1", bewerbung_id=bewerbung_id,
             body="Könnten Sie ein Zeugnis nachreichen?")
    await user.open("/antworten")
    await user.should_see("1 Antwort wartet auf dein Urteil")
    await user.should_see("Zeugnis nachreichen")
    for label in ("Absage", "Einladung", "Eingang", "Sonstiges"):
        await user.should_see(label)


async def test_a_settled_row_names_how_it_was_matched_and_by_whom(
        user: User, con):
    bewerbung_id = _application(con)
    _inbound(con, "m-1", bewerbung_id=bewerbung_id, needs_review=0,
             classification="absage", classified_by="rules")
    await user.open("/antworten")
    await user.should_see("Eingeordnet")
    await user.should_see("automatisch")
    await user.should_see("Beispiel GmbH")


async def test_hostile_mail_renders_as_text_not_markup(user: User, con):
    bewerbung_id = _application(con)
    hostile = "<img src=x onerror=alert(1)> **fett** [link](https://x)"
    _inbound(con, "m-1", bewerbung_id=bewerbung_id, subject=hostile,
             body=hostile)
    await user.open("/antworten")
    # the literal characters are on screen — nothing interpreted them
    await user.should_see("<img src=x onerror=alert(1)>")


# --------------------------------------------------------------------------
# actions settle rows
# --------------------------------------------------------------------------
async def test_his_verdict_settles_the_row_and_the_status(user: User, con):
    bewerbung_id = _application(con)
    row_id = _inbound(con, "m-1", bewerbung_id=bewerbung_id,
                      body="Wir haben noch eine Frage.")
    await user.open("/antworten")
    await user.should_see("wartet auf dein Urteil")

    buttons = [b for b in user.find("Einladung").elements]
    assert buttons, "the Einladung verdict button is not on screen"
    user.find("Einladung").click()
    await user.should_see("Nichts wartet auf dein Urteil")

    row = db.get_email_log(con, row_id)
    assert (row["classification"], row["needs_review"]) == ("einladung", 0)
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Einladung"


# --------------------------------------------------------------------------
# structural rules
# --------------------------------------------------------------------------
def _module_tree() -> ast.Module:
    path = pathlib.Path(antworten.__file__)
    return ast.parse(path.read_text(encoding="utf-8"))


def test_no_markup_sink_ever_touches_mail_content():
    """ui.label escapes; ui.markdown and ui.html do not. Mail is hostile
    input, so the whole module simply may not contain those sinks — the rule
    is structural because nothing about the page looks different when a
    future edit reaches for markdown to make a mail 'prettier'."""
    forbidden = []
    labels = 0
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "ui"):
                if node.func.attr in ("markdown", "html"):
                    forbidden.append((node.func.attr, node.lineno))
                if node.func.attr == "label":
                    labels += 1
    assert forbidden == [], forbidden
    assert labels >= 10, "the scan found too few labels to be looking at " \
                         "the real module"


def test_the_loader_reads_its_signature_first():
    """Beside the shared AST rule in test_live.py: the page's own loader
    must snapshot the signature before the rows it describes."""
    tree = _module_tree()
    load = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "_load")
    first_statement = load.body[0]
    while isinstance(first_statement, ast.With):
        first_statement = first_statement.body[0]
    assert isinstance(first_statement, ast.Assign)
    call = first_statement.value
    assert isinstance(call, ast.Call)
    assert getattr(call.func, "id", "") == "_signature"
