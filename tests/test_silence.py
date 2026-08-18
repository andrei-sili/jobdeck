"""An application nobody answered closes itself, and says only that.

The rule writes a status without anyone watching, so every arm is pinned:
what counts as silence, what restarts the clock, what must never be closed,
and — the reason for a status of its own — that the register's response rate
does not move when it fires.
"""

import datetime

import pytest

from jobdeck import db
from jobdeck.constants import BEANTWORTET_STATUS, OFFENE_STATUS, STATUS_RANK
from jobdeck.services import silence


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
    """A machine answering is not somebody answering — but it does prove
    someone is there, so it restarts the clock like any other contact."""
    bid = _application(con, days=90)
    _inbound(con, bid, "auto", days=80)
    con.commit()

    silence._close_silent()

    assert db.get_bewerbung(con, bid)["status"] == "Keine Antwort"


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

    assert report["off"] is True
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
