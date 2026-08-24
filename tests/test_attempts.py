"""Reservations: the claim that makes two applications to one company impossible.

The property under test is not "the gate refuses" — the old gate refused too.
It is that the refusal holds across connections while a provider call is in
flight, which is the window that produced a second application before.
"""

import datetime
import sqlite3
import threading

import pytest

from jobdeck import attempts, db, identity

NOW = "2026-08-24T10:00:00"


def _job(con, *, company="Beispiel GmbH", title="Software Engineer", external_id="a"):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": external_id, "title": title,
        "company": company, "url": "https://example.invalid/1"})
    con.commit()
    return db.get_job(con, job_id)


def _applied(con, *, company="Beispiel GmbH", position="", sent_on="2026-08-10"):
    """An application in the ledger, with its attempt, as the migration makes."""
    bew = db.add_bewerbung(con, {
        "gesendet_am": sent_on, "firma": company, "kanal": "E-Mail",
        "status": "Gesendet"})
    con.execute(
        "INSERT INTO application_attempts (idempotency_key, state, company,"
        " company_key, position, channel, bewerbung_id, created_at, updated_at)"
        " VALUES (?, 'recorded', ?, ?, ?, 'E-Mail', ?, ?, ?)",
        (f"bewerbung:{bew}", company, company.casefold(), position, bew, NOW, NOW))
    con.commit()
    return bew


# --------------------------------------------------------------------------
# The reservation itself
# --------------------------------------------------------------------------
def test_a_clean_company_can_be_reserved(con):
    job = _job(con)
    ok, decision = attempts.reserve(con, job, "E-Mail", now=NOW)
    con.commit()
    assert ok is True and decision.verdict == identity.ALLOW
    row = con.execute("SELECT * FROM application_attempts").fetchone()
    assert row["idempotency_key"] == f"job:{job['id']}"
    assert row["state"] == attempts.RESERVED
    assert row["company"] == "Beispiel GmbH"
    assert row["company_key"] == "beispiel gmbh"
    assert row["position"] == "Software Engineer"
    assert row["channel"] == "E-Mail"


def test_the_same_posting_cannot_be_reserved_twice(con):
    job = _job(con)
    assert attempts.reserve(con, job, "E-Mail", now=NOW)[0] is True
    con.commit()
    ok, decision = attempts.reserve(con, job, "Online-Portal", now=NOW)
    assert ok is False and decision.verdict == identity.RESERVED
    assert con.execute(
        "SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 1


def test_a_reservation_holds_the_whole_company_not_just_the_posting(con):
    """The race that produced two applications: an e-mail send in flight for
    one posting while the form path records another at the same employer."""
    in_flight = _job(con, title="Software Engineer", external_id="a")
    other = _job(con, title="Data Engineer", external_id="b")
    assert attempts.reserve(con, in_flight, "E-Mail", now=NOW)[0] is True
    con.commit()

    ok, decision = attempts.reserve(con, other, "Online-Portal", now=NOW)

    assert ok is False
    assert decision.verdict == identity.RESERVED
    assert decision.reservation_key == f"job:{in_flight['id']}"


def test_a_reservation_is_visible_to_another_connection(con, data_dir):
    """A row, not a process-local lock: the refusal must survive the boundary
    a background thread's own connection puts between the two paths."""
    job = _job(con, title="Software Engineer", external_id="a")
    other = _job(con, title="Data Engineer", external_id="b")
    con.execute("BEGIN IMMEDIATE")
    attempts.reserve(con, job, "E-Mail", now=NOW)
    con.commit()

    with db.db() as second:
        ok, decision = attempts.reserve(second, other, "Online-Portal", now=NOW)

    assert ok is False and decision.verdict == identity.RESERVED


def test_releasing_frees_the_company_and_the_key_is_reusable(con):
    """He abandoned a form here and came back. A fresh row would leave the old
    key taken and refuse the retry for ever."""
    job = _job(con)
    attempts.reserve(con, job, "Online-Portal", now=NOW)
    attempts.release(con, attempts.key_for_job(job["id"]), NOW)
    con.commit()

    ok, _ = attempts.reserve(con, job, "Online-Portal", now="2026-08-25T09:00:00")

    assert ok is True
    rows = con.execute("SELECT state, updated_at FROM application_attempts").fetchall()
    assert len(rows) == 1, "the revived attempt must not become a second row"
    assert rows[0]["state"] == attempts.RESERVED
    assert rows[0]["updated_at"] == "2026-08-25T09:00:00"


def test_only_a_live_reservation_becomes_a_record(con):
    job = _job(con)
    key = attempts.key_for_job(job["id"])
    attempts.reserve(con, job, "E-Mail", now=NOW)
    attempts.release(con, key, NOW)
    attempts.record(con, key, 99, NOW)
    con.commit()
    row = con.execute("SELECT * FROM application_attempts").fetchone()
    assert row["state"] == attempts.RELEASED and row["bewerbung_id"] is None


def test_recording_points_the_attempt_at_the_application(con):
    job = _job(con)
    key = attempts.key_for_job(job["id"])
    assert attempts.reserve(con, job, "E-Mail", now=NOW)[0] is True
    bew = db.add_bewerbung(con, {"gesendet_am": "2026-08-24", "firma": "Beispiel GmbH",
                                 "kanal": "E-Mail", "status": "Gesendet"})
    attempts.record(con, key, bew, "2026-08-24T10:05:00")
    con.commit()
    row = con.execute("SELECT * FROM application_attempts"
                      " WHERE idempotency_key=?", (key,)).fetchone()
    assert row["state"] == attempts.RECORDED and row["bewerbung_id"] == bew
    assert row["updated_at"] == "2026-08-24T10:05:00"


def test_an_application_taken_back_releases_its_attempt(con):
    job = _job(con)
    key = attempts.key_for_job(job["id"])
    assert attempts.reserve(con, job, "Online-Portal", now=NOW)[0] is True
    bew = db.add_bewerbung(con, {"gesendet_am": "2026-08-24", "firma": "Beispiel GmbH",
                                 "kanal": "Online-Portal", "status": "Gesendet"})
    attempts.record(con, key, bew, NOW)
    attempts.unrecord(con, bew, "2026-08-24T11:00:00")
    con.commit()
    row = con.execute("SELECT * FROM application_attempts"
                      " WHERE idempotency_key=?", (key,)).fetchone()
    assert row["state"] == attempts.RELEASED
    assert row["bewerbung_id"] is None, (
        "the pointer is a foreign key into the row that is about to go"
    )


# --------------------------------------------------------------------------
# The cooling-off window, read through the database
# --------------------------------------------------------------------------
def test_a_recent_application_holds_the_company_back(con):
    sent = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
    _applied(con, position="AI & Backend Engineer", sent_on=sent)
    job = _job(con, title="Software Engineer")

    ok, decision = attempts.reserve(con, job, "E-Mail", now=NOW)

    assert ok is False and decision.verdict == identity.COOLING_OFF
    assert decision.position == "AI & Backend Engineer"


def test_the_company_opens_again_once_the_window_passes(con):
    sent = (datetime.date.today() - datetime.timedelta(days=61)).isoformat()
    _applied(con, position="AI & Backend Engineer", sent_on=sent)
    job = _job(con, title="Software Engineer")
    assert attempts.reserve(con, job, "E-Mail", now=NOW)[0] is True


def test_the_position_comes_from_the_attempt_and_the_rest_from_the_ledger(con):
    """The ledger stays the source of truth for "an application went out", so
    dropping the new table degrades the gate to today's behaviour."""
    bew = _applied(con, company="Müller & Co", position="Python Entwickler")
    (application,) = attempts.applications(con)
    assert application.id == bew
    assert application.company == "Müller & Co"
    assert application.position == "Python Entwickler"


def test_a_republication_is_refused_whatever_the_window_says(con):
    sent = (datetime.date.today() - datetime.timedelta(days=400)).isoformat()
    _applied(con, position="Software Engineer", sent_on=sent)
    job = _job(con, title="Software Engineer")

    ok, decision = attempts.reserve(con, job, "E-Mail", now=NOW)

    assert ok is False and decision.verdict == identity.BLOCKED_REPUBLICATION


# --------------------------------------------------------------------------
# The candidate's recorded override
# --------------------------------------------------------------------------
def test_an_override_lifts_a_cooling_off_window_and_is_written_down(con):
    sent = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
    _applied(con, position="AI & Backend Engineer", sent_on=sent)
    job = _job(con, title="Software Engineer")

    ok, _ = attempts.reserve(con, job, "E-Mail", override=True,
                             override_evidence="applied 10 days ago", now=NOW)
    con.commit()

    assert ok is True
    row = con.execute("SELECT * FROM application_attempts WHERE state='reserved'"
                      ).fetchone()
    assert row["override_confirmed_at"] == NOW
    assert row["override_evidence"] == "applied 10 days ago"


def test_an_override_cannot_lift_a_republication(con):
    """Not his to overrule: a second application to the very same position is
    a mistake no confirmation makes reasonable."""
    _applied(con, position="Software Engineer")
    job = _job(con, title="Software Engineer")
    ok, decision = attempts.reserve(con, job, "E-Mail", override=True, now=NOW)
    assert ok is False and decision.verdict == identity.BLOCKED_REPUBLICATION


def test_an_override_cannot_lift_a_live_reservation(con):
    """The other path's message may already be leaving."""
    first = _job(con, title="Software Engineer", external_id="a")
    second = _job(con, title="Data Engineer", external_id="b")
    attempts.reserve(con, first, "E-Mail", now=NOW)
    con.commit()
    ok, decision = attempts.reserve(con, second, "Online-Portal",
                                    override=True, now=NOW)
    assert ok is False and decision.verdict == identity.RESERVED


def test_an_allowed_reservation_records_no_override(con):
    job = _job(con)
    attempts.reserve(con, job, "E-Mail", override=True,
                     override_evidence="not needed", now=NOW)
    con.commit()
    row = con.execute("SELECT * FROM application_attempts").fetchone()
    assert row["override_confirmed_at"] == ""
    assert row["override_evidence"] == ""


# --------------------------------------------------------------------------
# A reservation that outlived its work
# --------------------------------------------------------------------------
def test_startup_frees_a_reservation_whose_process_died(con):
    job = _job(con)
    attempts.reserve(con, job, "E-Mail", now=NOW)
    con.commit()

    freed = attempts.reconcile_interrupted(con, "2026-08-24T12:00:00")
    con.commit()

    assert freed == 1
    assert con.execute("SELECT state FROM application_attempts").fetchone()[0] == (
        attempts.RELEASED
    )


def test_startup_keeps_the_reservation_of_a_send_still_in_flight(con):
    """A stuck `sending` claim is already a decision for him, not a retry. Its
    company stays held until he resolves it."""
    job = _job(con)
    db.upsert_draft(con, job["id"], {"status": "sending"})
    attempts.reserve(con, job, "E-Mail", now=NOW)
    con.commit()

    assert attempts.reconcile_interrupted(con, NOW) == 0
    assert con.execute("SELECT state FROM application_attempts").fetchone()[0] == (
        attempts.RESERVED
    )


def test_startup_leaves_recorded_attempts_alone(con):
    _applied(con)
    assert attempts.reconcile_interrupted(con, NOW) == 0
    assert con.execute("SELECT state FROM application_attempts").fetchone()[0] == (
        attempts.RECORDED
    )


# --------------------------------------------------------------------------
# The window as a setting
# --------------------------------------------------------------------------
def test_the_window_defaults_to_sixty_days(con):
    assert attempts.cooldown_days(con) == 60


@pytest.mark.parametrize("stored, expected", [("14", 14), ("0", 0),
                                              ("-5", 0), ("nonsense", 60),
                                              ("", 60)])
def test_the_window_setting_has_one_deterministic_reading(con, stored, expected):
    db.set_setting(con, attempts.COOLDOWN_SETTING, stored)
    con.commit()
    assert attempts.cooldown_days(con) == expected


def test_a_zero_window_stops_holding_companies_back(con):
    db.set_setting(con, attempts.COOLDOWN_SETTING, "0")
    _applied(con, position="AI & Backend Engineer",
             sent_on=datetime.date.today().isoformat())
    con.commit()
    job = _job(con, title="Software Engineer")
    assert attempts.reserve(con, job, "E-Mail", now=NOW)[0] is True


# --------------------------------------------------------------------------
# Two real threads, one company
# --------------------------------------------------------------------------
def test_two_threads_racing_for_one_company_admit_exactly_one(con, data_dir):
    """The acceptance criterion, driven rather than argued: concurrent e-mail
    and form operations admit one attempt for the same company."""
    first = _job(con, title="Software Engineer", external_id="a")
    second = _job(con, title="Data Engineer", external_id="b")
    start = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def attempt(job, channel):
        start.wait(timeout=5)
        try:
            with db.db() as own:
                own.execute("BEGIN IMMEDIATE")
                ok, _ = attempts.reserve(own, job, channel, now=NOW)
        except sqlite3.OperationalError:      # lost the write lock entirely
            ok = False
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=attempt, args=(first, "E-Mail")),
               threading.Thread(target=attempt, args=(second, "Online-Portal"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert results.count(True) == 1, f"expected exactly one winner, got {results}"
    assert con.execute(
        "SELECT COUNT(*) FROM application_attempts WHERE state='reserved'"
    ).fetchone()[0] == 1


def test_a_posting_reports_its_own_attempt_in_flight(con):
    """A screen asking about a posting whose own send is running must not be
    told it may apply — the gate and the screen ask the same function."""
    job = _job(con)
    attempts.reserve(con, job, "E-Mail", now=NOW)
    con.commit()
    decision = attempts.decide_for_job(con, job)
    assert decision.verdict == identity.RESERVED
    assert decision.reservation_key == attempts.key_for_job(job["id"])


def test_a_released_attempt_no_longer_speaks_for_the_posting(con):
    job = _job(con)
    attempts.reserve(con, job, "E-Mail", now=NOW)
    attempts.release(con, attempts.key_for_job(job["id"]), NOW)
    con.commit()
    assert attempts.decide_for_job(con, job).verdict == identity.ALLOW


def test_a_posting_keeps_its_single_attempt_even_after_the_board_retitles_it(con):
    """The key is the POSTING, so its one attempt is spent whatever the title
    now says. Reachable: boards re-scrape and retitle, and with the window
    switched off the policy has nothing left to say about the company.

    Without the explicit refusal the insert would hit the UNIQUE constraint,
    and an IntegrityError out of a NiceGUI handler is a log line and a dead
    button, not a message."""
    db.set_setting(con, attempts.COOLDOWN_SETTING, "0")
    job = _job(con, title="Software Engineer")
    key = attempts.key_for_job(job["id"])
    attempts.reserve(con, job, "Online-Portal", now=NOW)
    bew = db.add_bewerbung(con, {"gesendet_am": "2026-08-24", "firma": "Beispiel GmbH",
                                 "kanal": "Online-Portal", "status": "Gesendet"})
    attempts.record(con, key, bew, NOW)
    con.execute("UPDATE jobs SET title=? WHERE id=?",
                ("Software Engineer, Infrastructure", job["id"]))
    con.commit()

    ok, decision = attempts.reserve(con, db.get_job(con, job["id"]), "E-Mail",
                                    now=NOW)

    assert ok is False
    assert decision.verdict == identity.BLOCKED_REPUBLICATION
    assert decision.reservation_key == key
    assert con.execute(
        "SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 1


# --------------------------------------------------------------------------
# The standing authorization: "apply here anyway"
# --------------------------------------------------------------------------
def test_an_authorization_lifts_the_window_for_that_posting_only(con):
    sent = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    _applied(con, position="AI & Backend Engineer", sent_on=sent)
    cleared = _job(con, title="Software Engineer", external_id="a")
    other = _job(con, title="Data Engineer", external_id="b")

    ok, decision = attempts.authorize(con, cleared, "er will trotzdem", NOW)
    con.commit()

    assert ok is True and decision.verdict == identity.COOLING_OFF
    assert attempts.decide_for_job(con, cleared).allowed is True
    assert attempts.decide_for_job(con, other).verdict == identity.COOLING_OFF


def test_an_authorization_survives_until_the_application_is_made(con):
    """By e-mail the letter is written first and sent minutes later. A
    confirmation that expired in between would refuse him at the last gate
    with no way to say yes again."""
    sent = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    _applied(con, position="AI & Backend Engineer", sent_on=sent)
    job = _job(con, title="Software Engineer")
    attempts.authorize(con, job, "er will trotzdem", NOW)
    con.commit()

    ok, _ = attempts.reserve(con, job, "E-Mail", now=NOW)

    assert ok is True
    row = con.execute("SELECT * FROM application_attempts WHERE job_id=?",
                      (job["id"],)).fetchone()
    assert row["state"] == attempts.RESERVED
    assert row["override_confirmed_at"] == NOW
    assert row["override_evidence"] == "er will trotzdem"


def test_an_authorization_is_written_down_with_its_evidence(con):
    sent = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    _applied(con, position="AI & Backend Engineer", sent_on=sent)
    job = _job(con, title="Software Engineer")

    attempts.authorize(con, job, "zuletzt am 19.08. beworben", NOW)
    con.commit()

    row = con.execute("SELECT * FROM application_attempts WHERE job_id=?",
                      (job["id"],)).fetchone()
    assert row["state"] == attempts.RELEASED, "it authorizes, it does not claim"
    assert row["override_confirmed_at"] == NOW
    assert row["override_evidence"] == "zuletzt am 19.08. beworben"
    assert row["position"] == "Software Engineer"


def test_nothing_can_be_authorized_that_the_window_did_not_refuse(con):
    """A republication is not the candidate's to overrule, and a live
    reservation may already be leaving."""
    _applied(con, position="Software Engineer")
    republication = _job(con, title="Software Engineer", external_id="a")
    ok, decision = attempts.authorize(con, republication, "trotzdem", NOW)
    assert ok is False and decision.verdict == identity.BLOCKED_REPUBLICATION
    assert con.execute(
        "SELECT COUNT(*) FROM application_attempts WHERE override_confirmed_at<>''"
    ).fetchone()[0] == 0


def test_a_free_company_needs_no_authorization(con):
    job = _job(con)
    ok, decision = attempts.authorize(con, job, "trotzdem", NOW)
    assert ok is False and decision.verdict == identity.ALLOW


def test_an_authorized_posting_leaves_the_held_pile_of_decisions(con):
    """The screens read `decisions_for_jobs`, so an authorization has to reach
    it too — otherwise the button unlocks and the warning stays."""
    sent = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    _applied(con, position="AI & Backend Engineer", sent_on=sent)
    cleared = _job(con, title="Software Engineer", external_id="a")
    other = _job(con, title="Data Engineer", external_id="b")
    attempts.authorize(con, cleared, "trotzdem", NOW)
    con.commit()

    found = attempts.decisions_for_jobs(con, [dict(cleared), dict(other)])

    assert cleared["id"] not in found
    assert found[other["id"]].verdict == identity.COOLING_OFF


def test_the_window_is_counted_from_the_last_contact_in_the_mailbox(con):
    """A receipt that arrived long after the application moves the window.

    Measured on a real corpus: a ledger row read as sent in June carried a
    JOIN receipt from August, so the company was offered again thirteen days
    after it had last been in touch."""
    import datetime

    bew = db.add_bewerbung(con, {"gesendet_am": "2026-06-12", "firma": "Beispiel GmbH",
                                 "kanal": "Online-Portal", "status": "Absage"})
    recent = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
    db.add_email_log(con, {
        "direction": "inbound", "from_addr": "no-reply@ats.example",
        "subject": "Eingang deiner Bewerbung", "bewerbung_id": bew,
        "classification": "eingang", "internal_date": f"{recent}T11:33:12"})
    con.commit()
    job = _job(con, title="Ganz andere Stelle")

    decision = attempts.decide_for_job(con, job)

    assert decision.verdict == identity.COOLING_OFF
    assert decision.sent_on == "2026-06-12", "the ledger date stays the evidence"
    assert decision.last_contact.startswith(recent)
    # …and the list filter agrees, or the screen and the gate part company
    held = {r[0] for r in con.execute(
        f"SELECT id FROM jobs WHERE {db.APPLIED_FIRM_SQL}",
        db.applied_firm_params(con))}
    assert job["id"] in held


def test_without_a_receipt_the_filter_still_counts_from_the_send_date(con):
    import datetime

    old = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    db.add_bewerbung(con, {"gesendet_am": old, "firma": "Beispiel GmbH",
                           "kanal": "Online-Portal", "status": "Absage"})
    con.commit()
    job = _job(con, title="Ganz andere Stelle")

    assert attempts.decide_for_job(con, job).verdict == identity.ALLOW
    held = {r[0] for r in con.execute(
        f"SELECT id FROM jobs WHERE {db.APPLIED_FIRM_SQL}",
        db.applied_firm_params(con))}
    assert job["id"] not in held
