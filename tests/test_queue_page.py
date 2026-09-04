"""Data-layer behaviour of the review queue page (no NiceGUI rendering)."""

import ast
import datetime
import pathlib

from jobdeck import db, identity
from jobdeck.services import drafting
from jobdeck.ui import draft_editor, helpers, live
from jobdeck.ui.pages import queue


def _job_with_draft(con, status="ready", **over):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "j1", "title": "Python Dev",
        "company": "Firma GmbH", "contact_email": "hr@firma.de",
    })
    values = dict(
        status=status, recipient="hr@firma.de",
        betreff="Bewerbung als Python Dev, K-17 – Max Muster",
        email_body="Guten Tag,\n\nanbei meine Bewerbung.",
        anschreiben_body="Sehr geehrte Damen und Herren,\n\nAbsatz.",
        pdf_path="/tmp/mappe.pdf",
    )
    values.update(over)
    db.upsert_draft(con, job_id, values)
    con.commit()
    return job_id


def _edit(text="Guten Tag,\n\nneuer Text."):
    return {"recipient": "hr@firma.de",
            "betreff": "Bewerbung als Python Dev, K-17 – Max Muster",
            "email_body": text,
            "anschreiben_body": "Sehr geehrte Damen und Herren,\n\nAbsatz."}


def test_editing_an_approved_draft_returns_it_to_ready(con, data_dir):
    """Approval is content-specific: auto-send must not transmit text the
    user changed after approving it."""
    job_id = _job_with_draft(con, status="approved")

    draft, error = draft_editor._save_draft(job_id, _edit(), clear_pdf=False)
    assert error == ""
    assert draft["status"] == "ready"
    assert draft["email_body"] == "Guten Tag,\n\nneuer Text."
    assert db.get_draft_by_job(con, job_id)["status"] == "ready"


def test_editing_a_ready_draft_keeps_it_ready(con, data_dir):
    job_id = _job_with_draft(con, status="ready")

    draft, error = draft_editor._save_draft(job_id, _edit(), clear_pdf=False)
    assert error == "" and draft["status"] == "ready"


def test_a_stale_dialog_cannot_rewrite_a_sent_draft(con, data_dir):
    """The editor may sit open while auto-send transmits: saving then would
    falsify the record of what actually went out."""
    job_id = _job_with_draft(con, status="sent")

    draft, error = draft_editor._save_draft(job_id, _edit(), clear_pdf=False)
    assert "no longer editable" in error
    assert draft["status"] == "sent"
    stored = db.get_draft_by_job(con, job_id)
    assert stored["email_body"] == "Guten Tag,\n\nanbei meine Bewerbung."


def test_a_stale_dialog_cannot_rewrite_a_sending_draft(con, data_dir):
    job_id = _job_with_draft(con, status="sending")

    _, error = draft_editor._save_draft(job_id, _edit(), clear_pdf=False)
    assert "no longer editable" in error
    assert db.get_draft_by_job(con, job_id)["email_body"] \
        == "Guten Tag,\n\nanbei meine Bewerbung."


def test_clear_pdf_drops_the_stale_mappe(con, data_dir):
    job_id = _job_with_draft(con)

    draft, error = draft_editor._save_draft(job_id, _edit(), clear_pdf=True)
    assert error == "" and draft["pdf_path"] == ""


def test_missing_draft_is_reported(con, data_dir):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "j9", "title": "x", "company": "y",
    })
    con.commit()

    draft, error = draft_editor._save_draft(job_id, _edit(), clear_pdf=False)
    assert draft is None and "gone" in error


def test_failed_drafts_are_reachable_in_the_open_filter(con, data_dir):
    """Their only other surface is the Job inbox's Draft button, which
    disappears once the job leaves status 'new'."""
    job_id = _job_with_draft(con, status="failed")
    db.set_job_status(con, job_id, "portal")
    con.commit()

    rows = db.list_drafts_with_jobs(con, queue.FILTER_STATUSES["open"])
    assert [r["status"] for r in rows] == ["failed"]


def test_the_queue_carries_the_liveness_of_the_posting_it_would_send_to(con,
                                                                       data_dir):
    """The queue is the last screen before a Bewerbung leaves. One real draft
    (job 18) was written and a 2.1 MB Mappe built for an ad gone forty days."""
    job_id = _job_with_draft(con)
    con.execute("UPDATE jobs SET liveness='gone', "
                "liveness_checked_at='2026-08-06T13:09:39' WHERE id=?", (job_id,))
    con.commit()
    row = db.list_drafts_with_jobs(con, ["ready"])[0]
    assert row["job_liveness"] == "gone"
    assert row["job_liveness_checked_at"].startswith("2026-08-06")

    # and a live posting says so rather than saying nothing
    con.execute("UPDATE jobs SET liveness='alive' WHERE id=?", (job_id,))
    con.commit()
    assert db.list_drafts_with_jobs(con, ["ready"])[0]["job_liveness"] == "alive"


# --------------------------------------------------------------------------
# A draft being written: the row existed for the ~60 s it took and no view
# listed it, so a second press was the only feedback the app gave.
# --------------------------------------------------------------------------
def test_a_draft_being_written_is_listed_while_it_is_written(con, data_dir):
    job_id = _job_with_draft(con, status="generating")
    rows = db.list_drafts_with_jobs(con, queue.FILTER_STATUSES["open"])
    assert [(r["job_id"], r["status"]) for r in rows] == [(job_id, "generating")]


def test_a_fresh_claim_promises_the_wait_and_says_it_updates_itself():
    now = datetime.datetime.now().isoformat(timespec="seconds")
    text, classes = queue.generating_line(now)
    assert "wird gerade geschrieben" in text
    assert "aktualisiert sich" in text
    assert "amber" not in classes


def test_an_abandoned_claim_says_so_instead_of_promising_a_minute():
    """The process can die mid-call. Past drafting's own claim timeout the Job
    inbox will already hand the posting to a new attempt, so the row must not
    keep telling him to wait."""
    stale = datetime.datetime.now() - datetime.timedelta(
        minutes=drafting.CLAIM_TIMEOUT_MIN + 2)
    text, classes = queue.generating_line(stale.isoformat(timespec="seconds"))
    assert "abgebrochen" in text
    # and it names a screen that exists: it used to send him to "Draft
    # application" in the "Job inbox", and neither has been called that for
    # two slices — the label there now depends on the posting's channel, so
    # the sentence names the SCREEN, in quotes, and says what to do there
    assert "„Stellen“" in text
    assert "Anschreiben" in text
    assert "amber" in classes
    # the number it prints is the real age, not a constant
    assert str(drafting.CLAIM_TIMEOUT_MIN + 2) in text


def test_an_unreadable_timestamp_is_not_evidence_of_a_dead_claim():
    text, _ = queue.generating_line("not a timestamp")
    assert "wird gerade geschrieben" in text
    assert queue.generating_line(None)[0] == text


def test_the_poll_rebuilds_the_page_only_when_the_drafts_changed(con, data_dir):
    """A rebuild collapses every open expansion. Polling every few seconds
    while he reads a different draft would be its own defect."""
    job_id = _job_with_draft(con, status="generating")
    before = queue._signature()
    assert queue._signature() == before

    db.upsert_draft(con, job_id, {"status": "ready"})
    con.commit()
    assert queue._signature() != before


def test_the_queue_asks_the_duplicate_gate_about_every_draft(con, data_dir):
    """Two of the five drafts waiting in his real queue were at companies the
    send path would refuse, and only the job inbox said so — on the form path,
    where the cockpit drafts, nothing did."""
    job_id = _job_with_draft(con)
    recent = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    db.add_bewerbung(con, {"gesendet_am": recent, "firma": "Firma GmbH",
                           "email": "", "kanal": "E-Mail", "status": "Absage"})
    con.commit()

    view = queue._load("open")

    decision = view["applied"][job_id]
    assert decision.verdict == identity.COOLING_OFF
    assert decision.sent_on == recent
    line = helpers.hold_line(decision, "Firma GmbH")
    assert "Firma GmbH" in line and "zurückgestellt" in line


def test_a_sent_draft_is_not_warned_about_its_own_application(con, data_dir):
    """It MADE that application: matching it against itself is an echo, and
    every row of the 'sent' tab would carry one."""
    job_id = _job_with_draft(con, status="sent")
    today = datetime.date.today().isoformat()
    row_id = db.add_bewerbung(con, {"gesendet_am": today,
                                    "firma": "Firma GmbH", "email": "hr@firma.de",
                                    "kanal": "E-Mail", "status": "Gesendet"})
    con.execute("UPDATE drafts SET bewerbung_id=? WHERE job_id=?", (row_id, job_id))
    con.execute("UPDATE jobs SET bewerbung_id=? WHERE id=?", (row_id, job_id))
    con.commit()

    assert queue._load("sent")["applied"] == {}


def test_a_second_application_at_that_company_still_warns(con, data_dir):
    """The echo is only the draft's OWN application; another one is exactly the
    thing the gate refuses."""
    job_id = _job_with_draft(con, status="sent")
    today = datetime.date.today()
    mine = db.add_bewerbung(con, {"gesendet_am": (today - datetime.timedelta(
        days=1)).isoformat(), "firma": "Firma GmbH", "email": "hr@firma.de",
        "kanal": "E-Mail", "status": "Gesendet"})
    db.add_bewerbung(con, {"gesendet_am": today.isoformat(),
                           "firma": "Firma GmbH", "email": "hr@firma.de",
                           "kanal": "E-Mail", "status": "Absage"})
    con.execute("UPDATE drafts SET bewerbung_id=? WHERE job_id=?", (mine, job_id))
    con.execute("UPDATE jobs SET bewerbung_id=? WHERE id=?", (mine, job_id))
    con.commit()

    assert queue._load("sent")["applied"][job_id].application_id != mine


def test_a_draft_at_an_untouched_company_carries_no_warning(con, data_dir):
    _job_with_draft(con)
    db.add_bewerbung(con, {"gesendet_am": "2026-06-12", "firma": "Andere AG",
                           "email": "", "kanal": "E-Mail", "status": "Absage"})
    con.commit()

    assert queue._load("open")["applied"] == {}


def test_the_ad_dying_under_a_queued_draft_is_part_of_the_signature(
        con, data_dir):
    """The pre-send warning is about the POSTING, and the liveness pass runs
    90 s after every start — i.e. exactly while he opens the queue and sends.
    A signature over the drafts alone could never show it."""
    job_id = _job_with_draft(con)
    before = queue._signature()

    db.set_job_liveness(con, job_id, "gone")
    con.commit()

    assert queue._signature() != before


def test_flipping_real_sending_in_another_tab_is_part_of_the_signature(
        con, data_dir):
    """The banner is this page's loudest safety statement; it must not keep
    saying TEST MODE after the switch was flipped elsewhere."""
    _job_with_draft(con)
    before = queue._signature()

    db.set_setting(con, "real_send_enabled", "1")
    con.commit()

    assert queue._signature() != before


def _repeating_timers(source: str) -> int:
    """`ui.timer(...)` calls that keep ticking — a `once=True` deferred loader
    is a different animal and stays allowed."""
    tree = ast.parse(source)
    found = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if not (isinstance(target, ast.Attribute) and target.attr == "timer"):
            continue
        once = any(kw.arg == "once" and getattr(kw.value, "value", False)
                   for kw in node.keywords)
        found += 0 if once else 1
    return found


def test_no_page_starts_a_repeating_timer_of_its_own():
    """Every self-refreshing page goes through ui/live.py, which owns the two
    properties a hand-rolled timer keeps getting wrong: rebuild only on a real
    change, and cancel on disconnect."""
    pages = pathlib.Path(queue.__file__).parent
    offenders = [p.name for p in sorted(pages.glob("*.py"))
                 if _repeating_timers(p.read_text())]
    assert offenders == []


def test_the_shared_timer_is_cancelled_when_the_page_goes_away():
    """NiceGUI reads a timer's parent slot BEFORE its own stop check
    (nicegui/timer.py, _run_in_loop), so a tick landing after the page is torn
    down raises rather than stopping — an ERROR traceback in his log every
    time he leaves the page. Seen in the running app, not in the suite."""
    source = pathlib.Path(live.__file__).read_text()
    assert source.count("ui.timer(") == 1
    tail = source[source.index("ui.timer("):]
    assert "on_disconnect(" in tail, "the timer outlives its page"
    assert "cancel()" in tail


def test_building_the_mappe_refreshes_what_the_send_pins(con, data_dir):
    """`create_mappe` ends in its own upsert_draft, and every upsert rewrites
    `updated_at` — so the snapshot the dialog pins with `expect=` went stale
    the moment the PDF was built, and the send that followed was refused with
    "the draft changed since you reviewed it". Every time, on the path the
    Stellen screen is built around: the last human gate before a real e-mail
    became a dialog he learns to dismiss and press again."""
    source = pathlib.Path(draft_editor.__file__).read_text()
    body = source[source.index("async def make_pdf"):]
    body = body[:body.index("def open_pdf")]
    assert "await run.io_bound(load, job_id)" in body, (
        "the editor keeps a pre-Mappe snapshot and pins it with expect=")
    assert "current.update(" in body


def test_a_draft_that_is_already_going_is_not_offered_a_send_button(con, data_dir):
    """A draft in `sending` or `sent` is the record of what went out. Offering
    "Jetzt senden" on one made the pre-send confirmation state "ECHTER Versand
    company" for a message the service refuses inside its claim — and that
    dialog's whole job is to be trustworthy."""
    assert "sending" not in draft_editor.EDITABLE_STATUS
    assert "sent" not in draft_editor.EDITABLE_STATUS
    source = pathlib.Path(draft_editor.__file__).read_text()
    guard = source[:source.index('"Jetzt senden"')]
    assert 'if row["status"] in EDITABLE_STATUS:' in guard


def test_the_editor_asks_the_database_rather_than_being_told(con, data_dir):
    """Handed in, this was stale by construction — twice. First a snapshot
    taken when the editor opened, then a callable over the caller's cached
    dict, which cannot refresh while a dialog is open. That window is exactly
    the one an auto-send tick uses. No caller may hand it in at all."""
    import ast
    ui_dir = pathlib.Path(draft_editor.__file__).parent
    checked = 0
    for path in sorted(ui_dir.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call)
                    and ast.unparse(node.func).endswith("open_editor")):
                assert not any(k.arg == "already_applied" for k in node.keywords), \
                    f"{path.name} tells the editor who has been applied to"
                checked += 1
    assert checked >= 2, "the scan found almost nothing"

    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "e1", "title": "Dev",
        "company": "Eine GmbH"})
    con.commit()
    assert draft_editor.applied_at_this_company(job_id) is None
    recent = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    blocking = db.add_bewerbung(con, {"gesendet_am": recent,
                                      "firma": "Eine GmbH", "kanal": "E-Mail",
                                      "status": "Absage"})
    con.commit()
    found = draft_editor.applied_at_this_company(job_id)
    assert found is not None and found.application_id == blocking
    assert draft_editor.applied_at_this_company(999999) is None


# --------------------------------------------------------------------------
# The one line an unopened row shows
# --------------------------------------------------------------------------
def test_every_draft_state_has_a_word_he_can_read():
    """The head is the only part an unopened row shows, and it carried the
    database's own vocabulary — 'ready', and after the form slice 'filed' —
    in the middle of a German screen."""
    from jobdeck.constants import DRAFT_STATUS
    for status in DRAFT_STATUS:
        assert status in queue.DRAFT_STATE, f"{status} has no German word"
        assert queue.draft_state(status) != status


def test_a_state_nobody_taught_it_is_shown_rather_than_swallowed():
    """Falling through to the raw name is ugly on purpose: a blank would hide
    a state entirely, and this row is the last stop before a send."""
    assert queue.draft_state("etwas_neues") == "etwas_neues"
    assert queue.draft_state(None) == ""


# --------------------------------------------------------------------------
# Which drafts each tab lists — untested until a new status needed classifying
# --------------------------------------------------------------------------
def test_a_letter_already_with_an_employer_never_waits_to_be_sent():
    """Adding 'filed' to the open filter puts a letter whose application is
    already out back into the stack that offers "Prüfen und senden" — one
    press from a second application at a firm that has one. The filter had no
    test at all, so that mutation was green."""
    assert "filed" not in queue.FILTER_STATUSES["open"]
    assert "sent" not in queue.FILTER_STATUSES["open"]
    assert "filed" in queue.FILTER_STATUSES["sent"]


def test_every_draft_status_is_reachable_through_exactly_one_tab():
    """A status in no tab is a letter with no screen — the shape that once
    made a posting undraftable, undiscardable and unpreparable for ever."""
    from jobdeck.constants import DRAFT_STATUS
    seen = [status for statuses in queue.FILTER_STATUSES.values()
            for status in statuses]
    assert sorted(seen) == sorted(set(seen)), "a status is listed twice"
    assert set(seen) == set(DRAFT_STATUS)


def test_the_two_ways_a_letter_can_have_gone_are_not_one_word():
    """"gesendet" beside a letter that went into an employer's upload field
    credits this app with an e-mail it never addressed."""
    assert queue.draft_state("filed") != queue.draft_state("sent")


# -- what the Postausgang says about a letter before he reads it --------------

def test_quality_lines_count_the_adverts_terms_and_name_the_tells():
    row = {"id": 1,
           "anschreiben_body": "Sehr geehrte Damen und Herren,\n\nMit Python und "
                               "Docker habe ich bei Beispiel GmbH gearbeitet. Die "
                               "Aufgabe reizt mich besonders.",
           "job_description": "Wir suchen Python, Docker und Kubernetes."}

    lines = queue.quality_lines(row, cv="Python · Kubernetes")

    assert lines == [
        ("Begriffe aus der Anzeige: 2 von 3 im Brief · 2 im Lebenslauf", ""),
        ("Floskel: „reizt mich besonders“", "warn"),
    ]


def test_a_draft_without_a_letter_gets_no_quality_line():
    assert queue.quality_lines({"id": 1, "anschreiben_body": "",
                                "job_description": "Python"}, cv="") == []


def test_the_loader_measures_every_row_against_the_other_rows(con, data_dir):
    """The opening comparison runs against the OTHER letters on file — a
    letter always opens like itself, and comparing it with itself would
    flag every draft."""
    job_a = _job_with_draft(con, anschreiben_body=(
        "Sehr geehrte Damen und Herren,\n\nBei Beispiel GmbH habe ich REST-APIs "
        "gebaut."))
    job_b = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "j2", "title": "Dev",
        "company": "Zweite GmbH", "description": "Python."})
    db.upsert_draft(con, job_b, {"status": "ready", "anschreiben_body": (
        "Sehr geehrte Frau Weber,\n\nBei Beispiel GmbH habe ich REST-APIs "
        "gebaut und getestet.")})
    con.commit()

    rows = queue._load("open")["drafts"]

    by_job = {r["job_id"]: r for r in rows}
    assert ("Beginnt wie ein früherer Brief", "warn") in by_job[job_a]["quality"]
    assert ("Beginnt wie ein früherer Brief", "warn") in by_job[job_b]["quality"]
    # one row alone has nobody to open like
    db.upsert_draft(con, job_b, {"status": "discarded"})
    con.commit()
    rows = queue._load("open")["drafts"]
    assert all(("Beginnt wie ein früherer Brief", "warn") not in r["quality"]
               for r in rows)
