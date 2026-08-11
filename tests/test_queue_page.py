"""Data-layer behaviour of the review queue page (no NiceGUI rendering)."""

import ast
import datetime
import pathlib

from jobdeck import db
from jobdeck.services import drafting
from jobdeck.ui import helpers, live
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

    draft, error = queue._save_draft(job_id, _edit(), clear_pdf=False)
    assert error == ""
    assert draft["status"] == "ready"
    assert draft["email_body"] == "Guten Tag,\n\nneuer Text."
    assert db.get_draft_by_job(con, job_id)["status"] == "ready"


def test_editing_a_ready_draft_keeps_it_ready(con, data_dir):
    job_id = _job_with_draft(con, status="ready")

    draft, error = queue._save_draft(job_id, _edit(), clear_pdf=False)
    assert error == "" and draft["status"] == "ready"


def test_a_stale_dialog_cannot_rewrite_a_sent_draft(con, data_dir):
    """The editor may sit open while auto-send transmits: saving then would
    falsify the record of what actually went out."""
    job_id = _job_with_draft(con, status="sent")

    draft, error = queue._save_draft(job_id, _edit(), clear_pdf=False)
    assert "no longer editable" in error
    assert draft["status"] == "sent"
    stored = db.get_draft_by_job(con, job_id)
    assert stored["email_body"] == "Guten Tag,\n\nanbei meine Bewerbung."


def test_a_stale_dialog_cannot_rewrite_a_sending_draft(con, data_dir):
    job_id = _job_with_draft(con, status="sending")

    _, error = queue._save_draft(job_id, _edit(), clear_pdf=False)
    assert "no longer editable" in error
    assert db.get_draft_by_job(con, job_id)["email_body"] \
        == "Guten Tag,\n\nanbei meine Bewerbung."


def test_clear_pdf_drops_the_stale_mappe(con, data_dir):
    job_id = _job_with_draft(con)

    draft, error = queue._save_draft(job_id, _edit(), clear_pdf=True)
    assert error == "" and draft["pdf_path"] == ""


def test_missing_draft_is_reported(con, data_dir):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "j9", "title": "x", "company": "y",
    })
    con.commit()

    draft, error = queue._save_draft(job_id, _edit(), clear_pdf=False)
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
    assert "abgebrochen" in text and "Draft" in text
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
    db.add_bewerbung(con, {"gesendet_am": "2026-06-12", "firma": "Firma GmbH",
                           "email": "", "kanal": "E-Mail", "status": "Absage"})
    con.commit()

    view = queue._load("open")

    assert view["applied"][job_id]["status"] == "Absage"
    assert "Firma GmbH" in helpers.applied_line(view["applied"][job_id])


def test_a_sent_draft_is_not_warned_about_its_own_application(con, data_dir):
    """It MADE that application: matching it against itself is an echo, and
    every row of the 'sent' tab would carry one."""
    job_id = _job_with_draft(con, status="sent")
    row_id = db.add_bewerbung(con, {"gesendet_am": "2026-08-11",
                                    "firma": "Firma GmbH", "email": "hr@firma.de",
                                    "kanal": "E-Mail", "status": "Gesendet"})
    con.execute("UPDATE drafts SET bewerbung_id=? WHERE job_id=?", (row_id, job_id))
    con.commit()

    assert queue._load("sent")["applied"] == {}


def test_a_second_application_at_that_company_still_warns(con, data_dir):
    """The echo is only the draft's OWN application; another one is exactly the
    thing the gate refuses."""
    job_id = _job_with_draft(con, status="sent")
    mine = db.add_bewerbung(con, {"gesendet_am": "2026-08-11",
                                  "firma": "Firma GmbH", "email": "hr@firma.de",
                                  "kanal": "E-Mail", "status": "Gesendet"})
    db.add_bewerbung(con, {"gesendet_am": "2026-08-12", "firma": "Firma GmbH",
                           "email": "hr@firma.de", "kanal": "E-Mail",
                           "status": "Absage"})
    con.execute("UPDATE drafts SET bewerbung_id=? WHERE job_id=?", (mine, job_id))
    con.commit()

    assert queue._load("sent")["applied"][job_id]["id"] != mine


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
