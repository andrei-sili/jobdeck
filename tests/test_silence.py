"""An application nobody answered closes itself, and says only that.

The rule writes a status without anyone watching, so every arm is pinned:
what counts as silence, what restarts the clock, what must never be closed,
and — the reason for a status of its own — that the register's response rate
does not move when it fires.
"""

import datetime
import sqlite3

import pytest

from jobdeck import db, gmail
from jobdeck.constants import (
    BEANTWORTET_STATUS,
    OFFENE_STATUS,
    STATUS_NO_ANSWER,
    STATUS_RANK,
)
from jobdeck.services import silence


@pytest.fixture(autouse=True)
def _mailbox_is_read(monkeypatch, data_dir, con):
    """Most tests are about WHO gets closed, which presupposes that replies
    are being read at all. The pass refuses to run otherwise — that guard has
    its own tests below."""
    monkeypatch.setattr(gmail, "can_read", lambda: True)
    db.set_setting(con, "replies_history_id", "12345")
    con.commit()


def _days_ago(n: int) -> str:
    return (datetime.datetime.now() - datetime.timedelta(days=n)).strftime(
        "%Y-%m-%d"
    )


def _stamp(n: int) -> str:
    return (datetime.datetime.now() - datetime.timedelta(days=n)).isoformat(
        timespec="seconds"
    )


def _application(con, firma="Beispiel GmbH", days=90, status="Gesendet",
                 kanal="E-Mail"):
    cur = con.execute(
        "INSERT INTO bewerbungen (gesendet_am, firma, kanal, status, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (_days_ago(days), firma, kanal, status, _stamp(days)),
    )
    return cur.lastrowid


def _inbound(con, bewerbung_id, classification, days=5):
    con.execute(
        "INSERT INTO email_log (direction, gmail_message_id, from_addr,"
        " bewerbung_id, classification, internal_date, created_at)"
        " VALUES ('inbound', ?, 'hr@beispiel.de', ?, ?, ?, ?)",
        (f"in-{bewerbung_id}-{classification}-{days}", bewerbung_id,
         classification, _stamp(days), _stamp(days)),
    )


# --- what the status itself must mean -------------------------------------

def test_no_answer_is_not_counted_as_a_reply():
    """The whole reason for a status of its own: the response rate must not
    grow because nobody answered."""
    assert silence.STATUS_NO_ANSWER not in BEANTWORTET_STATUS
    assert silence.STATUS_NO_ANSWER not in OFFENE_STATUS


def test_a_real_verdict_outranks_silence():
    """An employer answering on day 65 must overwrite it by itself."""
    assert STATUS_RANK["Einladung"] > STATUS_RANK[silence.STATUS_NO_ANSWER]
    assert STATUS_RANK["Absage"] > STATUS_RANK[silence.STATUS_NO_ANSWER]
    # ...and it must be writable over both open statuses
    assert STATUS_RANK[silence.STATUS_NO_ANSWER] > STATUS_RANK["Gesendet"]
    assert STATUS_RANK[silence.STATUS_NO_ANSWER] > STATUS_RANK["In Bearbeitung"]


# --- who gets closed -------------------------------------------------------

def test_a_long_silent_application_closes(data_dir, con):
    bid = _application(con, days=90)
    con.commit()

    report = silence._close_silent()

    assert report["closed"] == ["Beispiel GmbH"]
    assert db.get_bewerbung(con, bid)["status"] == "Keine Antwort"


def test_an_application_inside_the_window_is_left_alone(data_dir, con):
    bid = _application(con, days=30)
    con.commit()

    assert silence._close_silent()["closed"] == []
    assert db.get_bewerbung(con, bid)["status"] == "Gesendet"


@pytest.mark.parametrize("kanal", ["E-Mail", "Online-Portal", "Initiativ"])
def test_every_channel_closes(data_dir, con, kanal):
    """A form application has no address to be answered at, which makes its
    silence more final rather than less."""
    _application(con, days=90, kanal=kanal)
    con.commit()

    assert silence._close_silent()["closed"] == ["Beispiel GmbH"]


def test_an_application_in_bearbeitung_also_closes(data_dir, con):
    bid = _application(con, days=90, status="In Bearbeitung")
    con.commit()

    silence._close_silent()

    assert db.get_bewerbung(con, bid)["status"] == "Keine Antwort"


@pytest.mark.parametrize("status", ["Absage", "Einladung", "Zurückgezogen",
                                    "Antwort erhalten"])
def test_a_closed_application_is_never_touched(data_dir, con, status):
    bid = _application(con, days=90, status=status)
    con.commit()

    silence._close_silent()

    assert db.get_bewerbung(con, bid)["status"] == status


# --- what silence means ----------------------------------------------------

def test_a_receipt_restarts_the_clock(data_dir, con):
    bid = _application(con, days=90)
    _inbound(con, bid, "eingang", days=3)
    con.commit()

    silence._close_silent()

    assert db.get_bewerbung(con, bid)["status"] == "Gesendet"


def test_an_out_of_office_answer_does_not_save_it(data_dir, con):
    """A machine answering is not somebody answering — the row still closes.

    The inbound sits at day 80 of a 90-day application, so it is past the
    window on either clock: this pins only that 'auto' does not EXEMPT a row.
    Which clock ran is the next test's job — the two were conflated here, and
    the conflation made both halves unfalsifiable."""
    bid = _application(con, days=90)
    _inbound(con, bid, "auto", days=80)
    con.commit()

    silence._close_silent()

    assert db.get_bewerbung(con, bid)["status"] == STATUS_NO_ANSWER


def test_an_out_of_office_answer_does_not_restart_the_clock(data_dir, con):
    """Only a receipt restarts it. An out-of-office says the reader is away,
    not that the application was seen — and `auto` is the same bucket
    `replies.is_auto_submitted` files any List-Unsubscribe mailing under, so
    letting it restart the clock would keep a row at a newsletter-sending
    employer open for ever."""
    bid = _application(con, days=900)
    _inbound(con, bid, "auto", days=10)
    con.commit()

    silence._close_silent()

    assert db.get_bewerbung(con, bid)["status"] == STATUS_NO_ANSWER


def test_only_a_receipt_restarts_the_clock(data_dir, con):
    """The pair to the test above, on the same dates: a receipt inside the
    window keeps the row open where an out-of-office does not."""
    bid = _application(con, days=900)
    _inbound(con, bid, "eingang", days=10)
    con.commit()

    silence._close_silent()

    assert db.get_bewerbung(con, bid)["status"] == "Gesendet"


@pytest.mark.parametrize("classification", ["einladung", "absage", "sonstige"])
def test_an_application_a_human_answered_is_never_closed(
    data_dir, con, classification
):
    """Closing this as unanswered would contradict what the employer said."""
    bid = _application(con, days=90)
    _inbound(con, bid, classification, days=80)
    con.commit()

    silence._close_silent()

    assert db.get_bewerbung(con, bid)["status"] == "Gesendet"


# --- the setting -----------------------------------------------------------

def test_the_window_is_his_to_set(data_dir, con):
    bid = _application(con, days=40)
    db.set_setting(con, silence.SETTING_DAYS, "30")
    con.commit()

    silence._close_silent()

    assert db.get_bewerbung(con, bid)["status"] == "Keine Antwort"


def test_zero_switches_the_rule_off(data_dir, con):
    bid = _application(con, days=900)
    db.set_setting(con, silence.SETTING_DAYS, "0")
    con.commit()

    report = silence._close_silent()

    assert report["blocked"] == silence.BLOCKED_OFF
    assert db.get_bewerbung(con, bid)["status"] == "Gesendet"


def test_an_unreadable_setting_falls_back_to_the_default(data_dir, con):
    db.set_setting(con, silence.SETTING_DAYS, "bald")
    con.commit()

    assert silence.configured_days(con) == 60


# --- the audit trail -------------------------------------------------------

def test_every_close_leaves_a_history_row_naming_the_source(data_dir, con):
    bid = _application(con, days=90)
    con.commit()

    silence._close_silent()

    rows = con.execute(
        "SELECT old_status, new_status, source, note FROM status_history"
        " WHERE bewerbung_id=?", (bid,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["old_status"] == "Gesendet"
    assert rows[0]["new_status"] == "Keine Antwort"
    assert rows[0]["source"] == "silence"
    assert "ohne Antwort" in rows[0]["note"]


def test_running_twice_closes_nothing_the_second_time(data_dir, con):
    _application(con, days=90)
    con.commit()

    assert silence._close_silent()["closed"] == ["Beispiel GmbH"]
    assert silence._close_silent()["closed"] == []


def test_the_response_rate_does_not_move_when_silence_closes(data_dir, con):
    """The measured reason for the whole design."""
    answered = _application(con, firma="Hat geantwortet GmbH", days=90,
                            status="Absage")
    _application(con, firma="Schweigt GmbH", days=90)
    con.commit()

    def rate() -> int:
        placeholders = ",".join("?" * len(BEANTWORTET_STATUS))
        return con.execute(
            f"SELECT COUNT(*) FROM bewerbungen WHERE status IN ({placeholders})",
            tuple(BEANTWORTET_STATUS),
        ).fetchone()[0]

    before = rate()
    silence._close_silent()

    assert rate() == before == 1
    assert db.get_bewerbung(con, answered)["status"] == "Absage"


def test_silence_reads_as_a_closed_question_on_the_register():
    """Not amber: amber on this screen means "still waiting for an answer",
    which is the one thing this status has settled."""
    from jobdeck.ui.pages.bewerbungen import _pill_class

    assert _pill_class(silence.STATUS_NO_ANSWER) == ""
    assert _pill_class("Gesendet") == "warn"


# --- the guard that makes silence mean silence -----------------------------

def test_nothing_closes_while_replies_are_not_being_read(
    data_dir, con, monkeypatch
):
    """The rule infers silence from the ABSENCE of inbound mail, and a
    disconnected Gmail looks exactly the same. Without this, the first pass
    after a revoked token would close every open application at once."""
    monkeypatch.setattr(gmail, "can_read", lambda: False)
    bid = _application(con, days=900)
    con.commit()

    report = silence._close_silent()

    assert report["blocked"] == silence.BLOCKED_UNREAD
    assert report["closed"] == []
    assert db.get_bewerbung(con, bid)["status"] == "Gesendet"


def test_nothing_closes_before_ingestion_has_ever_run(data_dir, con):
    """The read scope alone is not evidence: a brand-new connection with no
    pass behind it has no inbound rows for the same reason a broken one has
    none."""
    db.set_setting(con, "replies_history_id", "")
    bid = _application(con, days=900)
    con.commit()

    report = silence._close_silent()

    assert report["blocked"] == silence.BLOCKED_UNREAD
    assert db.get_bewerbung(con, bid)["status"] == "Gesendet"


def test_an_unclassifiable_reply_blocks_the_close(data_dir, con):
    """An inbound the classifier could not place means "an employer wrote and
    we do not know what they said" — the opposite of knowing nobody did."""
    bid = _application(con, days=900)
    _inbound(con, bid, "", days=800)
    con.commit()

    silence._close_silent()

    assert db.get_bewerbung(con, bid)["status"] == "Gesendet"


# --- one bad row must not cost the others ----------------------------------

def test_a_row_that_raises_does_not_throw_away_the_closes_before_it(
    data_dir, con, monkeypatch
):
    first = _application(con, firma="Alpha GmbH", days=900)
    _application(con, firma="Beta GmbH", days=890)
    con.commit()

    real = db.set_status
    calls = {"n": 0}

    def exploding(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "set_status", exploding)
    report = silence._close_silent()

    assert report["closed"] == ["Alpha GmbH"]
    assert report["failed"] == ["Beta GmbH"]
    # the first close is committed, not rolled back with the failure
    assert db.get_bewerbung(con, first)["status"] == STATUS_NO_ANSWER


def test_an_application_without_a_company_name_does_not_break_the_pass(
    data_dir, con
):
    """`bewerbungen.firma` is nullable in the legacy table, and the name only
    ever reaches a log line."""
    cur = con.execute(
        "INSERT INTO bewerbungen (gesendet_am, firma, kanal, status, created_at)"
        " VALUES (?, NULL, 'E-Mail', 'Gesendet', ?)",
        (_days_ago(900), _stamp(900)))
    con.commit()

    report = silence._close_silent()

    assert report["failed"] == []
    assert db.get_bewerbung(con, cur.lastrowid)["status"] == STATUS_NO_ANSWER


# --- the register accounts for every row -----------------------------------

def test_the_register_accounts_for_the_rows_it_closed(data_dir, con):
    """42 open + 27 answered out of 84 leaves 15 rows in no line at all, on
    the one screen whose whole value is that its numbers are honest."""
    from jobdeck.services import register

    view = {"apps": [{"status": "Gesendet"}, {"status": "Absage"},
                     {"status": STATUS_NO_ANSWER}], "applied": 3}
    steps = {s.key: s.count for s in register.ledger(view)}

    assert steps["ohne_antwort"] == 1
    assert steps["offen"] + steps["beantwortet"] + steps["ohne_antwort"] \
        == steps["register"]


def test_the_register_says_nothing_when_nothing_was_closed(data_dir, con):
    from jobdeck.services import register

    view = {"apps": [{"status": "Gesendet"}], "applied": 1}
    assert "ohne_antwort" not in {s.key for s in register.ledger(view)}


# --- one clock, not two ----------------------------------------------------

def test_the_screen_counts_silence_the_way_the_rule_counts_it(data_dir, con):
    """Measured on the real register before this was fixed: thirteen of
    fifty-seven open rows printed a number the rule did not use — one of them
    69 days beside a threshold of 60, and still open, with nothing on the page
    explaining why."""
    import datetime

    from jobdeck.services import register

    bid = _application(con, days=90)
    _inbound(con, bid, "eingang", days=20)
    con.commit()

    apps = [dict(r) for r in db.list_bewerbungen(con)]
    waiting = register.silence(apps, 60, datetime.date.today())

    # the screen says 20 days, because that is when the employer last spoke
    assert [w.days for w in waiting] == [20]
    assert waiting[0].overdue is False
    # ...and the rule agrees: it does not close it at 60
    assert db.silent_applications(con, 60) == []


def test_the_screen_still_dates_a_row_nobody_ever_wrote_back_to(data_dir, con):
    import datetime

    from jobdeck.services import register

    _application(con, days=90)
    con.commit()

    apps = [dict(r) for r in db.list_bewerbungen(con)]
    waiting = register.silence(apps, 60, datetime.date.today())

    assert [w.days for w in waiting] == [90]
    assert waiting[0].overdue is True


# --- the arms nothing was watching ----------------------------------------

def test_the_window_closes_on_the_day_it_is_reached_and_not_before(
    data_dir, con
):
    """Day 59 stays, day 60 goes.

    Note for anyone mutating this: `>=` against `>` is EQUIVALENT here, not a
    gap in the test. `gesendet_am` is a date (midnight) and the comparison is
    against a moment, so the difference is never exactly the threshold — it
    was 60.4966 when this was written. The property worth holding is the day
    the row flips, and that is what this asserts.
    """
    early = _application(con, firma="Neunundfünfzig GmbH", days=59)
    due = _application(con, firma="Sechzig GmbH", days=60)
    con.commit()

    closed = {r["id"] for r in db.silent_applications(con, 60)}

    assert closed == {due}
    assert early not in closed


def test_the_clock_takes_the_NEWEST_contact_not_the_oldest(data_dir, con):
    """MAX against MIN: with two receipts, the oldest would make the row look
    long silent and close it while an employer wrote last week."""
    bid = _application(con, days=200)
    _inbound(con, bid, "eingang", days=190)
    _inbound(con, bid, "eingang", days=5)
    con.commit()

    assert db.silent_applications(con, 60) == []
    row = db.silent_applications(con, 4)[0]
    assert row["last_contact"][:10] == _stamp(5)[:10]


def test_the_longest_silence_is_offered_first(data_dir, con):
    """The pass writes statuses in this order and the log names them in it —
    a register drained oldest-first is the one he can reason about."""
    _application(con, firma="Zwei Monate GmbH", days=65)
    _application(con, firma="Ein Jahr GmbH", days=365)
    _application(con, firma="Drei Monate GmbH", days=95)
    con.commit()

    assert [r["firma"] for r in db.silent_applications(con, 60)] == [
        "Ein Jahr GmbH", "Drei Monate GmbH", "Zwei Monate GmbH"]


def test_the_channel_is_not_part_of_the_decision(data_dir, con):
    """The parametrised channel test above passes three values through one
    code path; this states the property that makes that acceptable."""
    import jobdeck.db as db_mod

    # `kanal` is SELECTed (callers report it) but must not be part of the
    # decision, which is everything from WHERE onwards.
    where = db_mod._SILENT_APPLICATIONS_SQL.split("WHERE", 1)[1]
    assert "kanal" not in where


# --- the entry point the scheduler actually calls --------------------------

async def test_the_scheduled_coroutine_really_runs_the_pass(data_dir, con):
    """Every other test calls the private `_close_silent`. This one drives the
    public coroutine the scheduler is wired to — rewriting it as `return {}`
    left the whole suite green."""
    bid = _application(con, days=900)
    con.commit()

    report = await silence.close_silent()

    assert report["closed"] == ["Beispiel GmbH"]
    assert db.get_bewerbung(con, bid)["status"] == STATUS_NO_ANSWER


async def test_the_pass_does_not_block_the_event_loop(data_dir, con):
    """It must go through a worker thread: the scheduler shares the loop with
    the UI, and this pass opens a database connection and writes."""
    import asyncio
    import threading

    seen = {}
    real = silence._close_silent

    def note():
        seen["thread"] = threading.current_thread()
        return real()

    loop_thread = threading.current_thread()
    silence._close_silent = note
    try:
        await silence.close_silent()
    finally:
        silence._close_silent = real

    assert seen["thread"] is not loop_thread
    assert asyncio.get_running_loop() is not None
