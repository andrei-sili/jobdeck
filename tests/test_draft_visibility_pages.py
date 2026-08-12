"""What the two pages actually SHOW about a draft, rendered for real.

Every other test module here works on the data layer or on pure helpers. That
left the visible half unproven: a review panel deleted the label that renders
the draft line, the code that relabels the pressed button, and the whole
polling mechanism, and all 873 tests stayed green. These drive the real
@ui.page functions in-process through NiceGUI's own user fixture.
"""

import asyncio
import datetime
import sys

import pytest
from nicegui.testing import User

from jobdeck import db
from jobdeck.services import drafting

pytest_plugins = ["nicegui.testing.user_plugin"]

pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_main.py")

DRAFT_BUTTON = "Draft application"


@pytest.fixture(autouse=True)
def _keep_the_package_importable():
    """Undo the one thing NiceGUI's reset does that reaches outside this module.

    On teardown `nicegui/testing/general.py` pops every registered page module
    AND ALL ITS PARENTS out of sys.modules — for a page in `jobdeck.ui.pages`
    that includes `jobdeck` itself, so the next test in the session re-imports
    a bare package and `monkeypatch.setattr("jobdeck.ai.scoring...")` fails
    with 'module jobdeck has no attribute ai'. Autouse, so this sets up first
    and therefore tears down last — after NiceGUI has done the popping."""
    saved = {name: mod for name, mod in sys.modules.items()
             if name == "jobdeck" or name.startswith("jobdeck.")}
    yield
    sys.modules.update(saved)


async def _open_pile(user: User, pile: str | None = None):
    """Switch the inbox to the 'already applied' pile through its real control."""
    from jobdeck.ui.pages import jobs as jobs_page
    toggle = next(iter(user.find(marker="pile-toggle").elements))
    toggle.set_value(pile if pile is not None else jobs_page.PILE_APPLIED)
    await asyncio.sleep(0.3)


def user_error_records(caplog):
    """The ERROR records the page logged during the test body."""
    return [r for r in caplog.get_records("call") if r.levelname == "ERROR"]


def _posting(con, company="Beispiel GmbH", **over):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": over.pop("ext", "e1"),
        "title": "Python Entwickler", "company": company,
        "url": "https://beispiel.example/1", **over,
    })
    con.commit()
    return job_id


def _claimed(con, job_id, minutes_ago: float):
    """A 'generating' row whose claim was taken `minutes_ago`."""
    db.upsert_draft(con, job_id, {"status": "generating"})
    stamp = (datetime.datetime.now()
             - datetime.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    con.execute("UPDATE drafts SET updated_at=? WHERE job_id=?", (stamp, job_id))
    con.commit()


# --------------------------------------------------------------------------
# Job inbox
# --------------------------------------------------------------------------
async def test_the_inbox_row_says_a_draft_is_being_written(user: User, con, data_dir):
    job_id = _posting(con)
    _claimed(con, job_id, minutes_ago=0.2)
    await user.open("/")
    await user.should_see("Beispiel GmbH")
    await user.should_see("wird gerade geschrieben")


async def test_the_draft_button_stands_down_only_while_the_claim_is_alive(
        user: User, con, data_dir):
    """Hiding it for as long as the row merely said 'generating' removed the
    ONLY surface that can trigger drafting's reclaim, so a draft abandoned by
    a crash became a posting that could never be drafted again."""
    job_id = _posting(con)
    _claimed(con, job_id, minutes_ago=0.2)
    await user.open("/")
    await user.should_not_see(DRAFT_BUTTON)

    # …and the moment the claim is old enough for _claim to take it over, the
    # button is back — this is the way out of an abandoned draft.
    con.execute("UPDATE drafts SET updated_at=? WHERE job_id=?", (
        (datetime.datetime.now()
         - datetime.timedelta(minutes=drafting.CLAIM_TIMEOUT_MIN + 1)
         ).isoformat(timespec="seconds"), job_id))
    con.commit()
    await user.open("/")
    await user.should_see(DRAFT_BUTTON)
    await user.should_see("abgebrochen")
    await user.should_not_see("etwa eine Minute")


@pytest.mark.parametrize("status, expected", [
    ("ready", "Entwurf fertig"),
    ("failed", "fehlgeschlagen"),
    ("sent", "Bewerbung gesendet"),
])
async def test_the_inbox_row_states_every_other_draft_state(
        user: User, con, data_dir, status, expected):
    job_id = _posting(con)
    db.upsert_draft(con, job_id, {"status": status})
    con.commit()
    await user.open("/")
    await user.should_see(expected)


async def test_a_discarded_draft_leaves_the_row_as_it_found_it(
        user: User, con, data_dir):
    job_id = _posting(con)
    db.upsert_draft(con, job_id, {"status": "discarded"})
    con.commit()
    await user.open("/")
    await user.should_see(DRAFT_BUTTON)
    await user.should_not_see("Entwurf")


# --------------------------------------------------------------------------
# Review queue
# --------------------------------------------------------------------------
async def test_the_queue_lists_a_draft_while_it_is_being_written(
        user: User, con, data_dir):
    job_id = _posting(con)
    _claimed(con, job_id, minutes_ago=0.2)
    await user.open("/queue")
    await user.should_see("wird geschrieben")
    await user.should_see("Beispiel GmbH")


async def test_the_queue_row_becomes_the_finished_draft_by_itself(
        user: User, con, data_dir, monkeypatch):
    """The feature's headline promise — 'Die Zeile aktualisiert sich von
    selbst'. Nothing exercised it, so the poll could be made permanently
    inert with the suite green."""
    from jobdeck.ui.pages import queue
    monkeypatch.setattr(queue, "GENERATING_POLL_SECONDS", 0.05)
    job_id = _posting(con)
    _claimed(con, job_id, minutes_ago=0.2)
    await user.open("/queue")
    await user.should_see("wird geschrieben")

    db.upsert_draft(con, job_id, {
        "status": "ready", "recipient": "jobs@beispiel.example",
        "betreff": "Bewerbung als Python Entwickler"})
    con.commit()
    await asyncio.sleep(0.4)  # several poll ticks, no interaction at all
    await user.should_see("Bewerbung als Python Entwickler")
    await user.should_not_see("wird geschrieben")


async def test_the_queue_does_not_rebuild_while_nothing_changes(
        user: User, con, data_dir, monkeypatch):
    """A rebuild collapses every open expansion and — before the dialogs moved
    out of the list — deleted an open editor. It must happen only on a real
    change."""
    from jobdeck.ui.pages import queue
    monkeypatch.setattr(queue, "GENERATING_POLL_SECONDS", 0.05)
    job_id = _posting(con)
    _claimed(con, job_id, minutes_ago=0.2)

    # A poll that finds nothing new reads the signature and stops there; only a
    # rebuild goes on to load the rows. Counting both tells the two apart
    # without reaching into the page's closures.
    polls, rebuilds = {"n": 0}, {"n": 0}
    for name, counter in (("_signature", polls), ("_load", rebuilds)):
        real = getattr(queue, name)

        def counting(*args, _real=real, _counter=counter):
            _counter["n"] += 1
            return _real(*args)

        monkeypatch.setattr(queue, name, counting)

    await user.open("/queue")
    await asyncio.sleep(0.4)
    assert polls["n"] > 2, "the poll never ran"
    assert rebuilds["n"] == 1, (
        f"the page rebuilt {rebuilds['n']} times with nothing changed — that "
        "collapses every open expansion he is reading")

    db.upsert_draft(con, job_id, {"status": "ready", "betreff": "Fertig"})
    con.commit()
    await asyncio.sleep(0.3)
    assert rebuilds["n"] == 2, "a real change did not rebuild the page"


async def test_an_abandoned_claim_tells_both_screens_the_same_thing(
        user: User, con, data_dir):
    """Two views of one row contradicting each other is what sent him to a
    screen whose button had been removed."""
    job_id = _posting(con)
    _claimed(con, job_id, minutes_ago=drafting.CLAIM_TIMEOUT_MIN + 5)
    await user.open("/queue")
    await user.should_see("abgebrochen")
    await user.should_not_see("Die Zeile aktualisiert sich")
    await user.open("/")
    await user.should_see("abgebrochen")


async def test_the_pressed_button_says_what_it_is_doing_for_the_whole_wait(
        user: User, con, data_dir, monkeypatch):
    """A minute is long enough that the faded 'Drafting application…' toast
    reads as 'nothing happened' — which is what made him press it twice. The
    relabel could be replaced with `pass` and the suite stayed green."""
    from jobdeck.ui.pages import jobs as jobs_page

    started, release = asyncio.Event(), asyncio.Event()

    async def slow_draft(job_id):
        started.set()
        await release.wait()
        return {"ok": False, "error": "stopped on purpose", "draft": None}

    monkeypatch.setattr(jobs_page.drafting, "draft_for_job", slow_draft)
    _posting(con)
    await user.open("/")
    await user.should_see(DRAFT_BUTTON)

    user.find(DRAFT_BUTTON).click()
    await asyncio.wait_for(started.wait(), timeout=2)
    await user.should_see("wird geschrieben…")
    await user.should_not_see(DRAFT_BUTTON)

    release.set()
    await asyncio.sleep(0.2)
    # …and the failure is REPORTED rather than swallowed by the refresh that
    # deleted the slot the handler was running in
    await user.should_see("stopped on purpose")


async def test_an_unexpected_failure_does_not_leave_the_button_dead(
        user: User, con, data_dir, monkeypatch, caplog):
    """drafting re-raises anything that is not an LLM error. Without a
    try/finally the row kept claiming a draft was being written, the list was
    never refreshed and the control stayed greyed out until a page reload —
    under a commit titled 'report a drafting failure instead of swallowing
    it'."""
    from jobdeck.ui.pages import jobs as jobs_page

    async def exploding_draft(job_id):
        raise RuntimeError("a posting field was not what the code expected")

    monkeypatch.setattr(jobs_page.drafting, "draft_for_job", exploding_draft)
    _posting(con)
    await user.open("/")
    user.find(DRAFT_BUTTON).click()
    await asyncio.sleep(0.3)
    await user.should_see(DRAFT_BUTTON)
    await user.should_not_see("wird geschrieben…")
    await user.should_see("unerwartet fehlgeschlagen")

    # The traceback must still reach the log — that is the only place the real
    # cause survives. Consumed here because NiceGUI's `user` fixture fails a
    # test on any ERROR record, and this one is the point of the test.
    logged = user_error_records(caplog)
    assert [r.message for r in logged] == ["drafting job 1 raised"]
    assert logged[0].exc_info[0] is RuntimeError
    caplog.get_records("call").clear()


# --------------------------------------------------------------------------
# "You already applied here" — computed, not remembered
# --------------------------------------------------------------------------
async def test_a_posting_at_a_firm_he_already_wrote_to_says_so(
        user: User, con, data_dir):
    """`jobs.duplicate_of` is written once, when the posting is discovered, so
    every application sent afterwards makes more inbox rows lie. Measured on
    his real data: 30 open postings were at firms the send gate would already
    refuse, and the top-ranked posting of all had had an Absage."""
    job_id = _posting(con, company="Beispiel GmbH")
    db.add_bewerbung(con, {"gesendet_am": "2026-06-12", "firma": "Beispiel GmbH",
                           "email": "", "kanal": "E-Mail", "status": "Absage"})
    con.commit()
    # nothing wrote jobs.duplicate_of — the posting still looks untouched
    assert con.execute("SELECT duplicate_of FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] is None

    # it is OUT of the working list: it can never become an application, which
    # is a fact about the posting, exactly like a score-0 mismatch
    await user.open("/")
    await user.should_not_see("Beispiel GmbH")
    await user.should_see("bei schon beworbenen Firmen hidden")

    # …and one click away, saying why, with nothing inviting him to spend a
    # draft on an application that can never be sent
    await _open_pile(user)
    await user.should_see("Beispiel GmbH")
    await user.should_see("bereits beworben")
    await user.should_see("Absage")
    await user.should_see("2026-06-12")
    await user.should_not_see(DRAFT_BUTTON)


async def test_the_decorated_spelling_is_covered_by_the_same_warning(
        user: User, con, data_dir):
    """The whole point of the norm fix, seen from the screen."""
    _posting(con, company="Beispiel® GmbH")
    db.add_bewerbung(con, {"gesendet_am": "2026-06-12", "firma": "Beispiel GmbH",
                           "email": "", "kanal": "E-Mail", "status": "Absage"})
    con.commit()
    await user.open("/")
    await _open_pile(user)
    await user.should_see("bereits beworben")


async def test_a_firm_he_never_wrote_to_is_left_alone(user: User, con, data_dir):
    _posting(con, company="Ganz Neue GmbH")
    db.add_bewerbung(con, {"gesendet_am": "2026-06-12", "firma": "Andere GmbH",
                           "email": "", "kanal": "E-Mail", "status": "Absage"})
    con.commit()
    await user.open("/")
    await user.should_not_see("bereits beworben")
    await user.should_see(DRAFT_BUTTON)


async def test_opening_a_draft_twice_leaves_one_dialog_behind(
        user: User, con, data_dir):
    """`overlay.clear()` could be deleted with the suite green while every
    draft left its dialog parented to the host for the page's lifetime —
    contradicting the comment that says it keeps exactly one alive."""
    from nicegui import ui

    job_id = _posting(con)
    db.upsert_draft(con, job_id, {
        "status": "ready", "betreff": "Bewerbung als Python Entwickler",
        "recipient": "jobs@beispiel.example"})
    con.commit()
    await user.open("/")

    def dialogs():
        return [e for e in user.client.elements.values()
                if isinstance(e, ui.dialog)]

    for _ in range(3):
        user.find(DRAFT_BUTTON).click()
        await asyncio.sleep(0.2)
        assert len(dialogs()) == 1, f"{len(dialogs())} dialogs are alive at once"
