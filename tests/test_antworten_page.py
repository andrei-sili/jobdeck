"""The Antworten screen's contract: honesty about the reader, hostile mail
stays text, and the review actions really settle rows.

Reply subjects and bodies are text ANYONE can send. The property most worth
pinning is therefore structural: this module may never hand mail content to
a markup sink — labels only — because nothing about the page looks different
when that breaks.
"""

import ast
import asyncio
import pathlib
import sys
import time

import pytest
from nicegui import ui
from nicegui.testing import User

from jobdeck import config, db, gmail
from jobdeck.services import replies as replies_service
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


async def _open_view(user: User, view_key: str):
    """Switch the list to a named view through its real control."""
    select = next(iter(user.find(marker="view-select").elements))
    select.set_value(view_key)
    await asyncio.sleep(0.3)


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
    await user.should_see("1 Vorgang wartet")
    # the first Vorgang is selected by itself, so the mail is being read and
    # every verdict is reachable without a click
    await user.should_see("Zeugnis nachreichen")
    for label in ("Absage", "Einladung", "Eingang", "Sonstiges"):
        await user.should_see(label)


async def test_a_settled_row_names_how_it_was_matched_and_by_whom(
        user: User, con):
    bewerbung_id = _application(con)
    _inbound(con, "m-1", bewerbung_id=bewerbung_id, needs_review=0,
             classification="absage", classified_by="rules")
    await user.open("/antworten")
    await _open_view(user, "eingeordnet")
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
    await user.should_see("1 Vorgang wartet")

    buttons = [b for b in user.find("Einladung").elements]
    assert buttons, "the Einladung verdict button is not on screen"
    user.find("Einladung").click()
    await user.should_see("Keine Antwort wartet auf dein Urteil")

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


async def test_an_automatically_filed_row_can_be_corrected(user: User, con):
    """The card promises 'ein Klick korrigiert sie'. Without a control on
    the settled rows that promise was false for exactly the rows it is
    about — the ones the rules filed by themselves."""
    bewerbung_id = _application(con)
    row_id = _inbound(con, "m-1", bewerbung_id=bewerbung_id, needs_review=0,
                      classification="absage", classified_by="rules")
    db.set_status(con, bewerbung_id, "Absage", source="reply_auto")
    con.commit()

    await user.open("/antworten")
    await _open_view(user, "eingeordnet")
    await user.should_see("automatisch")
    user.find("Korrigieren").click()
    await user.should_see("Wie war diese Antwort gemeint?")
    # `kind=` matters: the ledger's honesty note contains the word
    # "Einladungen", so a bare text match hits that label before the button.
    # a marker, not a text match: the ledger's honesty note contains the word
    # "Einladungen", and the rail carries buttons of its own
    user.find(marker="correct-einladung").click()
    # waits for the row to be REDRAWN — 'Eingeordnet' is the view title and
    # is on screen either way, so asserting on it would race the handler
    await user.should_see("bestätigt")

    row = db.get_email_log(con, row_id)
    assert (row["classification"], row["classified_by"]) \
        == ("einladung", "reply_manual")
    # his verdict outranks the reader's — equal rank does not block a human
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Einladung"


async def test_a_receipt_attached_to_his_own_record_offers_no_undo(
        user: User, con):
    bewerbung_id = _application(con)
    _inbound(con, "m-1", bewerbung_id=bewerbung_id, needs_review=0,
             classification="eingang", classified_by="rules",
             matched_by="receipt_known")
    await user.open("/antworten")
    await user.should_not_see("Rückgängig")


async def test_the_page_offers_to_re_read_what_it_skipped(
        user: User, data_dir, con, monkeypatch):
    """A skipped message is invisible by design — only its opaque id is
    kept. The page has to say how many there are, or the one honest limit of
    the whole feature is undiscoverable from inside it."""
    config.TOKEN_PATH.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gmail, "can_read", lambda: True)
    with db.db() as write:
        for n in range(3):
            db.add_email_log(write, {"direction": "inbound_ignored",
                                     "gmail_message_id": f"skip-{n}"})
    await user.open("/antworten")
    await user.should_see("3 Nachrichten wurden keiner Bewerbung zugeordnet")
    await user.should_see("Alle Nachrichten neu prüfen")


async def test_the_page_stays_quiet_when_nothing_was_skipped(
        user: User, data_dir, con, monkeypatch):
    config.TOKEN_PATH.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gmail, "can_read", lambda: True)
    await user.open("/antworten")
    await user.should_not_see("Alle Nachrichten neu prüfen")


# --------------------------------------------------------------------------
# Vorgänge: the pure shaping
# --------------------------------------------------------------------------
def _mail(id_, **kw) -> dict:
    row = {"id": id_, "bewerbung_id": None, "bewerbung_firma": "",
           "job_company": "", "from_addr": "", "subject": "", "matched_by": "",
           "classification": "", "classified_by": "", "internal_date": "",
           "bewerbung_status": "", "job_id": None, "matched_note": "",
           "body_text": "", "snippet": "", "created_at": ""}
    row.update(kw)
    return row


def test_one_firms_mails_become_one_vorgang():
    """The measurement that decided the shape: 42 waiting mails touch 30
    applications, and seven firms hold several. As a card each, one firm's
    four replies sat at four unrelated positions in a 7 000 px column."""
    rows = [
        _mail(1, bewerbung_id=7, bewerbung_firma="Beispiel GmbH",
              internal_date="2026-08-01T09:00:00"),
        _mail(2, bewerbung_id=7, bewerbung_firma="Beispiel GmbH",
              internal_date="2026-08-15T09:00:00"),
        _mail(3, bewerbung_id=9, bewerbung_firma="Andere AG",
              internal_date="2026-08-10T09:00:00"),
    ]

    groups = antworten.vorgaenge(rows)

    assert [g["firma"] for g in groups] == ["Beispiel GmbH", "Andere AG"]
    assert groups[0]["count"] == 2
    # the NEWEST mail decides what the row says
    assert groups[0]["lead"]["id"] == 2
    assert groups[0]["mails"][1]["id"] == 1


def test_a_mail_with_no_application_is_its_own_vorgang():
    """A receipt proposal has no application yet — grouping them together by
    their shared NULL would put unrelated employers under one row."""
    rows = [_mail(1, job_id=4, from_addr="a@x.example"),
            _mail(2, job_id=5, from_addr="b@y.example")]

    groups = antworten.vorgaenge(rows)

    assert len(groups) == 2


def test_an_invitation_is_lifted_over_newer_mail():
    """The one thing here with a date and a person waiting for an answer.
    Drawn in arrival order it sat between two receipts looking like them."""
    rows = [
        _mail(1, bewerbung_id=7, bewerbung_firma="Alt",
              classification="einladung", internal_date="2026-08-01T09:00:00"),
        _mail(2, bewerbung_id=9, bewerbung_firma="Neu",
              classification="eingang", internal_date="2026-08-17T09:00:00"),
    ]

    groups = antworten.vorgaenge(rows)

    assert [g["firma"] for g in groups] == ["Alt", "Neu"]
    assert groups[0]["invitation"] is True


def test_the_closed_view_and_the_open_view_partition_the_shelf():
    """23 of his 42 waiting mails hang off closed applications, so the two
    views must together account for every Vorgang and overlap in none."""
    rows = [_mail(1, bewerbung_id=1, bewerbung_status="Gesendet"),
            _mail(2, bewerbung_id=2, bewerbung_status="Absage"),
            _mail(3, bewerbung_id=3, bewerbung_status="In Bearbeitung"),
            _mail(4, bewerbung_id=4, bewerbung_status="Einladung"),
            _mail(5, job_id=9)]  # no application at all
    groups = antworten.vorgaenge(rows)

    offen = [g for g in groups if antworten.in_view(g, "offen")]
    closed = [g for g in groups if antworten.in_view(g, "abgeschlossen")]

    assert len(offen) == 3          # Gesendet, In Bearbeitung, no application
    assert len(closed) == 2         # Absage, Einladung
    assert len(offen) + len(closed) == len(groups)
    assert all(antworten.in_view(g, "alle") for g in groups)


def test_a_mail_without_an_application_counts_as_open_not_closed():
    """An empty status is the most open thing there is: nothing has been
    decided about it yet. Filed under 'abgeschlossen' it would be swept away
    by the bulk gesture without ever being read."""
    group = antworten.vorgaenge([_mail(1, job_id=4)])[0]

    assert group["closed"] is False
    assert antworten.in_view(group, "offen") is True


# --------------------------------------------------------------------------
# the guard, stated the same way in both places
# --------------------------------------------------------------------------
def test_the_screens_guard_matches_the_services_guard_exactly(con):
    """`kept_status` tells the button what will happen; `resolve_review`
    makes it happen. Two copies of one rule is the drift this project keeps
    paying for, so they are pinned equal over every pair — a mutation to
    either has to turn this red."""
    statuses = ["", "Gesendet", "In Bearbeitung", "Antwort erhalten",
                "Einladung", "Absage", "Zurückgezogen"]
    for status in statuses:
        for classification in antworten.VERDICTS:
            bewerbung_id = db.add_bewerbung(con, {
                "gesendet_am": "2026-08-11", "firma": "F", "kanal": "E-Mail",
                "status": status})
            row_id = db.add_email_log(con, {
                "direction": "inbound", "gmail_message_id": f"m{bewerbung_id}",
                "bewerbung_id": bewerbung_id, "needs_review": 1})
            con.commit()

            predicted = antworten.kept_status(status, classification)
            outcome = replies_service.resolve_review(row_id, classification)

            assert bool(predicted) is (not outcome["status_written"]
                                       and bool(outcome["kept"])), \
                (status, classification, predicted, outcome)
            if predicted:
                assert outcome["kept"] == predicted, (status, classification)


def test_the_button_states_its_consequence_before_it_is_pressed():
    """He triages from the keyboard, so a consequence only a mouse can
    reveal is a consequence he never sees — hence a label, never a tooltip."""
    closed = antworten.vorgaenge(
        [_mail(1, bewerbung_id=1, bewerbung_status="Absage")])[0]
    open_one = antworten.vorgaenge(
        [_mail(2, bewerbung_id=2, bewerbung_status="Gesendet")])[0]
    orphan = antworten.vorgaenge([_mail(3, job_id=9)])[0]

    assert antworten.verdict_reason(open_one, "absage") \
        == "ändert den Stand: Gesendet → Absage"
    assert antworten.verdict_reason(closed, "eingang") == "Stand bleibt Absage"
    assert antworten.verdict_reason(closed, "absage") \
        == "ändert nichts — der Stand ist schon Absage"
    assert "keine Bewerbung" in antworten.verdict_reason(orphan, "eingang")


def test_the_row_leads_with_the_fact_that_decides_whether_a_press_is_safe():
    """The line is nowrap with an ellipsis, so the deciding fact must not be
    last. Stellen leads with age because its row's job is different."""
    group = antworten.vorgaenge([
        _mail(1, bewerbung_id=1, bewerbung_status="Absage",
              matched_by="name", internal_date="2026-08-16T14:00:00")])[0]

    assert antworten.row_meta(group).startswith("Stand: Absage")
    assert "Firmenname" in antworten.row_meta(group)


def test_a_name_match_says_it_is_a_resemblance():
    """The name arm is a similarity, not an identification — it is why 26 of
    his 42 rows are proposals rather than writes, and the reader has to say
    so where he decides."""
    group = antworten.vorgaenge([
        _mail(1, bewerbung_id=1, bewerbung_status="Gesendet",
              matched_by="name")])[0]

    notes = dict((kind, text) for text, kind in antworten.reader_notes(group))
    assert "keine Identifikation" in notes["warn"]


def test_a_closed_application_is_named_as_closed_in_the_reader():
    group = antworten.vorgaenge([
        _mail(1, bewerbung_id=1, bewerbung_status="Absage")])[0]

    notes = dict((kind, text) for text, kind in antworten.reader_notes(group))
    assert "bereits abgeschlossen" in notes["danger"]


# --------------------------------------------------------------------------
# the keyboard
# --------------------------------------------------------------------------
def test_rows_are_options_never_buttons():
    """`ui.keyboard`'s `ignore` list contains "button" and the browser focuses
    a button on mousedown, so building rows as buttons kills the keyboard at
    the first click. That is how it broke on Stellen, and the page looks
    identical when it happens."""
    tree = _module_tree()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "element"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ui"
                and node.args
                and isinstance(node.args[0], ast.Constant)):
            assert node.args[0].value != "button", \
                f"ui.element('button') at line {node.lineno} would disable " \
                "the keyboard for every row below it"


def test_the_keyboard_is_inert_while_a_dialog_is_open():
    """A dialog sits OVER the list: reaching for its close button and hitting
    'x' must not file the Vorgang underneath it."""
    source = pathlib.Path(antworten.__file__).read_text(encoding="utf-8")
    handler = source[source.index("async def on_key"):]
    handler = handler[:handler.index("\n        # ---")]
    for guard in ("event.action.keydown", "event.action.repeat",
                  "live.dialog_open()", "event.modifiers.ctrl"):
        assert guard in handler, f"the keyboard lost its {guard} guard"


async def test_j_and_k_move_the_cursor_without_rebuilding_the_list(
        user: User, con):
    first = _application(con, firma="Aaa GmbH")
    second = _application(con, firma="Bbb AG")
    _inbound(con, "m-1", bewerbung_id=first, subject="Erste",
             matched_by="thread")
    _inbound(con, "m-2", bewerbung_id=second, subject="Zweite",
             matched_by="thread")
    await user.open("/antworten")
    await user.should_see("2 Vorgänge warten")
    rows = _option_rows(user)
    assert len(rows) == 2
    selected = [r for r in rows if r._props.get("aria-selected") == "true"]
    assert len(selected) == 1
    original = list(rows)

    await antworten_key(user, "j")

    rows = _option_rows(user)
    # the SAME elements, re-propped rather than rebuilt — and they must STAY
    # the same across the watcher's next tick, which is what fails when the
    # selection is part of the list's redraw signature
    assert len(rows) == 2
    assert all(r in original for r in rows), \
        "the list was rebuilt by a cursor move — the scroll position is gone"
    assert [r._props.get("aria-selected") for r in rows] == ["false", "true"]


def _option_rows(user: User) -> list:
    """The list's rows in DOM order.

    `user.find(...).elements` is a SET, so reading it as a sequence gives an
    order that changes between runs — which is a flaky test, not a moving
    cursor. NiceGUI ids increase with creation, so they are the DOM order."""
    return sorted((e for e in user.find(marker=None).elements
                   if e._props.get("role") == "option"),
                  key=lambda e: e.id)


async def antworten_key(user: User, key: str, *, action: str = "keydown",
                        repeat: bool = False, ctrlKey: bool = False) -> None:
    """A key, through NiceGUI's own inbound event path.

    Not the page's handler called directly: the framework is what decides a
    keystroke typed into the search box never reaches the page, and that rule
    is exactly what a test of the keyboard has to exercise."""
    from nicegui.events import GenericEventArguments
    keyboard = next(e for e in user.client.elements.values()
                    if isinstance(e, ui.keyboard))
    with user.client:
        keyboard._handle_key(GenericEventArguments(
            sender=keyboard, client=user.client, args={
                "action": action, "repeat": repeat, "key": key,
                "code": f"Key{key.upper()}", "location": 0,
                "altKey": False, "ctrlKey": ctrlKey, "metaKey": False,
                "shiftKey": False}))
    await asyncio.sleep(0.4)


async def test_enter_takes_the_machines_proposal(user: User, con):
    """25 of his 42 waiting rows already carry a proposal, so ⏎ is the key
    that empties the pile. It takes the proposal and nothing else — a
    keystroke must never invent a verdict."""
    bewerbung_id = _application(con)
    row_id = _inbound(con, "m-1", bewerbung_id=bewerbung_id,
                      classification="absage", classified_by="rules")
    await user.open("/antworten")
    await user.should_see("1 Vorgang wartet")

    await antworten_key(user, "Enter")

    row = db.get_email_log(con, row_id)
    assert (row["classification"], row["needs_review"]) == ("absage", 0)
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Absage"


async def test_enter_on_a_row_with_no_proposal_does_nothing(user: User, con):
    """17 of his 42 have no classification at all. ⏎ there would have to
    guess, and a guess written as a status is the whole failure mode this
    screen exists to avoid."""
    bewerbung_id = _application(con)
    row_id = _inbound(con, "m-1", bewerbung_id=bewerbung_id)
    await user.open("/antworten")
    await user.should_see("1 Vorgang wartet")

    await antworten_key(user, "Enter")

    row = db.get_email_log(con, row_id)
    assert (row["classification"], row["needs_review"]) == ("", 1)
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"


async def test_enter_never_reopens_a_closed_application(user: User, con):
    """The 8 rows measured on his real shelf: a receipt proposal under an
    application already at Absage. A keystroke must never be the thing that
    changes a status the buttons would have guarded."""
    bewerbung_id = _application(con)
    db.set_status(con, bewerbung_id, "Absage", source="user")
    row_id = _inbound(con, "m-1", bewerbung_id=bewerbung_id,
                      classification="eingang", classified_by="rules")
    con.commit()
    await user.open("/antworten")
    await _open_view(user, "alle")
    await user.should_see("1 Vorgang wartet")

    await antworten_key(user, "Enter")

    # the mail is filed — it is not asked about again ...
    assert db.get_email_log(con, row_id)["needs_review"] == 0
    # ... and the closed application stands
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Absage"


async def test_x_files_the_mail_without_touching_the_register(user: User, con):
    bewerbung_id = _application(con)
    row_id = _inbound(con, "m-1", bewerbung_id=bewerbung_id)
    await user.open("/antworten")
    await user.should_see("1 Vorgang wartet")

    await antworten_key(user, "x")

    row = db.get_email_log(con, row_id)
    assert (row["needs_review"], row["bewerbung_id"]) == (0, None)
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"


async def test_a_held_key_files_one_verdict_not_thirty(user: User, con):
    """Held keys repeat ~30 times a second. Without the repeat guard, resting
    a finger on ⏎ would file a dozen applications."""
    bewerbung_id = _application(con)
    row_id = _inbound(con, "m-1", bewerbung_id=bewerbung_id,
                      classification="absage", classified_by="rules")
    await user.open("/antworten")
    await user.should_see("1 Vorgang wartet")

    await antworten_key(user, "Enter", repeat=True)

    assert db.get_email_log(con, row_id)["needs_review"] == 1
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"


# --------------------------------------------------------------------------
# the June archive: one gesture, no status
# --------------------------------------------------------------------------
async def test_the_closed_view_files_its_whole_pile_and_writes_no_status(
        user: User, con):
    """23 of his 42 waiting mails hang off applications he closed himself.
    They are cleared in one gesture — and deliberately not as a bulk verdict:
    `resolve_review` writes as 'reply_manual', which the anti-downgrade rank
    exempts, so "confirm all twelve" would be twelve unguarded status
    writes."""
    closed_ids, rows = [], []
    for index in range(3):
        bewerbung_id = _application(con, firma=f"Firma {index}")
        db.set_status(con, bewerbung_id, "Absage", source="user")
        closed_ids.append(bewerbung_id)
        rows.append(_inbound(con, f"m-{index}", bewerbung_id=bewerbung_id,
                             classification="eingang", classified_by="rules"))
    open_id = _application(con, firma="Noch offen")
    open_row = _inbound(con, "m-open", bewerbung_id=open_id)
    con.commit()

    await user.open("/antworten")
    await _open_view(user, "abgeschlossen")
    await user.should_see("3 Vorgänge warten")
    user.find("Alle ablegen").click()
    await user.should_see("Kein Stand wird geändert")
    user.find("Ablegen", kind=ui.button).click()
    await user.should_see("3 Mails abgelegt — kein Stand wurde geändert")

    for row_id in rows:
        assert db.get_email_log(con, row_id)["needs_review"] == 0
    for bewerbung_id in closed_ids:
        assert db.get_bewerbung(con, bewerbung_id)["status"] == "Absage"
    # the open pile is untouched — the gesture belongs to one view
    assert db.get_email_log(con, open_row)["needs_review"] == 1


async def test_the_open_view_says_how_many_it_is_hiding(user: User, con):
    """Nothing is deleted and nothing is silently dropped: the default view
    names what it left out, the same rule the job list follows."""
    closed_id = _application(con, firma="Zu")
    db.set_status(con, closed_id, "Absage", source="user")
    _inbound(con, "m-1", bewerbung_id=closed_id)
    _inbound(con, "m-2", bewerbung_id=_application(con, firma="Offen"))
    con.commit()

    await user.open("/antworten")

    await user.should_see("1 Vorgang wartet")
    await user.should_see("abgeschlossenen Bewerbung ausgeblendet")


async def test_a_dismissed_mail_can_be_put_back_on_the_shelf(user: User, con):
    """`dismiss_review` keeps the row, so this was a one-way door with no
    schema reason to be one."""
    bewerbung_id = _application(con)
    row_id = _inbound(con, "m-1", bewerbung_id=bewerbung_id, needs_review=0)
    db.link_reply_bewerbung(con, row_id, None)
    con.commit()

    await user.open("/antworten")
    await _open_view(user, "eingeordnet")
    await user.should_see("Abgelegt")
    user.find("Zurück zur Prüfung").click()
    await user.should_see("Wieder auf dem Prüfstapel")

    assert db.get_email_log(con, row_id)["needs_review"] == 1


def test_the_lists_redraw_signature_holds_no_rendered_clock():
    """`row_meta` runs the date through `clock`, which is relative to now. A
    fingerprint built from the rendered line therefore changes on its own, and
    the watcher rebuilds the list under him minutes into a sitting — the same
    class as the unsigned `opened_at` that twice tore down the posting he was
    reading. Pinned structurally: measuring it would need a fake clock, and
    the failure is invisible either way."""
    fingerprint = next(
        node for node in _module_tree().body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_group_fingerprint")
    called = {n.func.id for n in ast.walk(fingerprint)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "row_meta" not in called, \
        "the list's fingerprint is derived from a rendered, clock-relative " \
        "line — it will change with no data change and rebuild the list"
    keys = {n.value for n in ast.walk(fingerprint)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "internal_date" in keys, \
        "the fingerprint must carry the FACT the rendered line comes from"


def test_moving_the_cursor_is_not_part_of_the_lists_redraw_signature():
    """Putting `selected` in the list state made every j rebuild the list on
    the watcher's next tick, throwing the scroll position away. The selection
    is reflected by rewriting two rows' props in place instead."""
    source = pathlib.Path(antworten.__file__).read_text(encoding="utf-8")
    assignment = source[source.index("            list_state = ("):]
    assignment = assignment[:assignment.index("\n            strip = (")]
    assert 'state["selected"]' not in assignment, assignment


async def test_an_invitation_is_marked_not_just_ranked_first(user: User, con):
    """His report on the first real read: the invitation WAS classified
    correctly and still sat in the pile looking like a receipt. Ranking it
    first is not enough — drawn like everything else it reads like everything
    else, in the list AND in the panel where he decides."""
    other = _application(con, firma="Andere AG")
    _inbound(con, "m-old", bewerbung_id=other, subject="Eingang",
             classification="eingang", classified_by="rules")
    invited = _application(con, firma="Wichtig GmbH")
    _inbound(con, "m-new", bewerbung_id=invited, subject="Vorstellungsgespräch",
             classification="einladung", classified_by="rules")

    await user.open("/antworten")

    await user.should_see("2 Vorgänge warten")
    # first in the list ...
    assert _option_rows(user)[0]._props.get("aria-selected") == "true"
    # ... and said out loud, in both places
    marked = [e for e in user.find(marker=None).elements
              if "jd-urgent-note" in (e._classes or [])]
    assert len(marked) >= 2, "the invitation is ranked but not marked"
    # ... and not shouted: a 45-character German sentence in small caps is
    # unreadable, and the project already had to un-shout its buttons once
    assert all("jd-urgent" not in (e._classes or []) for e in marked)


async def test_the_cursor_lands_where_he_acted_not_back_at_the_top(
        user: User, con):
    """Found by the review panel, and it is the defect that would have cost
    him the most: after each verdict the cursor jumped back to the first row,
    so clearing 42 Vorgänge meant walking down the list again after every
    single one — O(n²) keystrokes on the screen built to stop exactly that.
    Stellen carries `prefer_index` for the same reason."""
    for index in range(4):
        _inbound(con, f"m-{index}",
                 bewerbung_id=_application(con, firma=f"Firma {index}"),
                 subject=f"Betreff {index}")
    con.commit()
    await user.open("/antworten")
    await user.should_see("4 Vorgänge warten")

    await antworten_key(user, "j")
    await antworten_key(user, "j")
    third = _option_rows(user)[2]
    assert third._props.get("aria-selected") == "true"
    subject_below = _option_rows(user)[3].default_slot.children[1] \
        .default_slot.children[1].text

    await antworten_key(user, "x")

    await user.should_see("3 Vorgänge warten")
    rows = _option_rows(user)
    selected = [r for r in rows if r._props.get("aria-selected") == "true"]
    assert len(selected) == 1
    assert rows.index(selected[0]) == 2, \
        "the cursor jumped away from where he acted"
    # and it is the row that TOOK the place, not some other one
    assert selected[0].default_slot.children[1].default_slot.children[1].text \
        == subject_below


async def test_a_second_keystroke_mid_write_cannot_file_the_same_mail_twice(
        user: User, con, monkeypatch):
    """Found by the review panel. A verdict is a write in a worker thread, and
    the row does not leave the list until it lands — so a second keystroke in
    that window reads the SAME selection, files the same mail again, and the
    row below it is silently skipped. On a 42-item sitting that is exactly the
    tempo he will type at."""
    from jobdeck.services import replies as service
    for index in range(3):
        _inbound(con, f"m-{index}",
                 bewerbung_id=_application(con, firma=f"Firma {index}"))
    con.commit()
    await user.open("/antworten")
    await user.should_see("3 Vorgänge warten")

    real, seen = service.dismiss_review, []

    def slow(email_log_id):
        seen.append(email_log_id)
        time.sleep(0.35)
        return real(email_log_id)

    monkeypatch.setattr(service, "dismiss_review", slow)

    first = asyncio.create_task(antworten_key(user, "x"))
    await asyncio.sleep(0.05)
    await antworten_key(user, "x")
    await first

    assert len(seen) == 1, f"the same mail was filed {len(seen)} times: {seen}"
    assert len(db.pending_review_replies(con)) == 2


async def test_alle_ablegen_files_what_the_list_shows_not_more(user: User, con):
    """Found by the review panel. The button was keyed on every closed
    Vorgang rather than on the filtered list, so with a search active "Alle
    ablegen" filed mail he could not see — and "Alle" meant something other
    than everything on screen, which is the one thing a bulk gesture may
    never do."""
    keep = _application(con, firma="Unsichtbar GmbH")
    db.set_status(con, keep, "Absage", source="user")
    keep_row = _inbound(con, "m-keep", bewerbung_id=keep)
    hit = _application(con, firma="Gesucht AG")
    db.set_status(con, hit, "Absage", source="user")
    hit_row = _inbound(con, "m-hit", bewerbung_id=hit)
    con.commit()

    await user.open("/antworten")
    await _open_view(user, "abgeschlossen")
    await user.should_see("2 Vorgänge warten")
    user.find(kind=ui.input).type("Gesucht")
    await asyncio.sleep(0.6)
    await user.should_see("1 Vorgang wartet")

    user.find("Alle ablegen").click()
    await user.should_see("Kein Stand wird geändert")
    user.find("Ablegen", kind=ui.button).click()
    # the message AFTER the write — "abgelegt" alone also matches the dialog
    await user.should_see("kein Stand wurde geändert")

    assert db.get_email_log(con, hit_row)["needs_review"] == 0
    assert db.get_email_log(con, keep_row)["needs_review"] == 1, \
        "a Vorgang the search had hidden was filed anyway"


async def test_what_files_itself_is_readable_while_he_triages(user: User, con):
    """The old page carried this sentence permanently. Moving it into the
    empty state alone would have meant the one screen that never shows it is
    the one he actually works on — and "what gets written without me" is not
    something the buttons' own consequences can say."""
    _inbound(con, "m-1", bewerbung_id=_application(con))
    con.commit()

    await user.open("/antworten")

    await user.should_see("1 Vorgang wartet")
    await user.should_see("trägt JobDeck selbst ein")


async def test_the_settled_view_does_not_claim_nothing_was_read(
        user: User, con):
    """The list pane carried the view's empty text — "Noch keine Antwort
    gelesen" — beside a panel listing the answers it had just read."""
    _inbound(con, "m-1", bewerbung_id=_application(con), needs_review=0,
             classification="absage", classified_by="rules")
    con.commit()

    await user.open("/antworten")
    await _open_view(user, "eingeordnet")

    await user.should_see("1 Antwort ist eingeordnet")
    assert not [e for e in user.find(marker=None).elements
                if "Noch keine Antwort gelesen" in str(getattr(e, "text", ""))]


async def test_the_page_and_the_rail_can_be_reconciled(user: User, con):
    """The rail counts MAILS ("N zu prüfen"), the page counts Vorgänge. With
    only one of the two figures on screen the two disagree out loud and
    neither explains why — 42 in the rail against 17 on the page, on his real
    corpus."""
    shared = _application(con, firma="Viele Mails GmbH")
    _inbound(con, "m-1", bewerbung_id=shared, subject="Erste")
    _inbound(con, "m-2", bewerbung_id=shared, subject="Zweite")
    _inbound(con, "m-3", bewerbung_id=_application(con, firma="Eine AG"))
    con.commit()

    await user.open("/antworten")

    await user.should_see("2 Vorgänge warten · 3 Mails insgesamt")


async def test_the_ledger_says_when_it_is_showing_only_the_newest(
        user: User, con):
    """It lists LEDGER_LIMIT rows. A ledger that quietly ends is worse than a
    short one that says so."""
    for index in range(antworten.LEDGER_LIMIT + 2):
        _inbound(con, f"m-{index}",
                 bewerbung_id=_application(con, firma=f"F{index}"),
                 needs_review=0, classification="absage",
                 classified_by="rules")
    con.commit()

    await user.open("/antworten")
    await _open_view(user, "eingeordnet")

    await user.should_see(f"die neuesten {antworten.LEDGER_LIMIT}")
