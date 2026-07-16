import asyncio
import datetime
from zoneinfo import ZoneInfo

import pytest

from jobdeck import config, db, gmail
from jobdeck.services import autosend, send

TEST_INBOX = "inbox@test.example"
TUESDAY_10 = datetime.datetime(2026, 7, 14, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))


@pytest.fixture(autouse=True)
def _fresh_lock(monkeypatch):
    monkeypatch.setattr(send, "_lock", asyncio.Lock())


@pytest.fixture()
def business_hours(monkeypatch):
    monkeypatch.setattr(autosend, "_now_berlin", lambda: TUESDAY_10)


@pytest.fixture()
def sent_messages(monkeypatch):
    calls = []

    def fake_send(message):
        calls.append(message)
        return (f"m-{len(calls)}", f"t-{len(calls)}")

    monkeypatch.setattr(gmail, "send_message", fake_send)
    return calls


def _setup_approved(con, tmp_path, auto_send=1, active=1, external_id="j1",
                    profile=None):
    """A profile (opted in or not) with a job and an approved draft."""
    if profile is None:
        profile = db.add_profile(con, {
            "name": "p", "keywords": "python", "auto_send": auto_send,
            "active": active,
        })
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": external_id, "title": "Python Dev",
        "company": f"Firma {external_id}", "contact_email": "hr@firma.de",
        "profile_id": profile,
    })
    pdf = tmp_path / f"mappe_{external_id}.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    db.upsert_draft(con, job_id, {
        "status": "approved", "recipient": "hr@firma.de",
        "betreff": "Bewerbung als Python Dev – Max Muster",
        "email_body": "Guten Tag,\n\nanbei meine Bewerbung.",
        "pdf_path": str(pdf),
    })
    con.commit()
    return job_id, profile


def _sendable(con):
    db.set_setting(con, "test_recipient", TEST_INBOX)
    con.commit()
    config.TOKEN_PATH.write_text("{}", encoding="utf-8")


# -- pacing gates ---------------------------------------------------------------
@pytest.mark.parametrize("now,reason", [
    (datetime.datetime(2026, 7, 18, 10, 0,
                       tzinfo=ZoneInfo("Europe/Berlin")), "Saturday"),
    (datetime.datetime(2026, 7, 14, 8, 59,
                       tzinfo=ZoneInfo("Europe/Berlin")), "too early"),
    (datetime.datetime(2026, 7, 14, 17, 0,
                       tzinfo=ZoneInfo("Europe/Berlin")), "too late"),
])
async def test_outside_business_hours_sends_nothing(
    con, tmp_path, monkeypatch, sent_messages, now, reason
):
    monkeypatch.setattr(autosend, "_now_berlin", lambda: now)
    _setup_approved(con, tmp_path)
    _sendable(con)

    result = await autosend.tick()
    assert result == {"sent": 0, "reason": "outside business hours"}
    assert sent_messages == []


async def test_spacing_window_blocks_until_due(
    con, tmp_path, business_hours, sent_messages
):
    _setup_approved(con, tmp_path)
    _sendable(con)
    later = (TUESDAY_10 + datetime.timedelta(minutes=5)).isoformat(
        timespec="seconds")
    db.set_setting(con, autosend.NEXT_SEND_KEY, later)
    con.commit()

    result = await autosend.tick()
    assert result["sent"] == 0 and "next send window" in result["reason"]
    assert sent_messages == []


async def test_garbage_next_send_state_does_not_wedge(
    con, tmp_path, business_hours, sent_messages
):
    _setup_approved(con, tmp_path)
    _sendable(con)
    db.set_setting(con, autosend.NEXT_SEND_KEY, "not a timestamp")
    con.commit()

    assert (await autosend.tick())["sent"] == 1


# -- eligibility ----------------------------------------------------------------
async def test_tick_sends_exactly_one_oldest_and_schedules_next(
    con, tmp_path, business_hours, sent_messages, monkeypatch
):
    monkeypatch.setattr(autosend.random, "uniform", lambda a, b: 10.0)
    first_job, profile = _setup_approved(con, tmp_path, external_id="j1")
    _setup_approved(con, tmp_path, external_id="j2", profile=profile)
    _sendable(con)

    result = await autosend.tick()
    assert result["sent"] == 1
    assert result["job_id"] == first_job  # oldest draft first
    assert result["test_mode"] is True
    assert len(sent_messages) == 1
    assert sent_messages[0]["To"] == TEST_INBOX

    expected_next = (TUESDAY_10 + datetime.timedelta(minutes=10)).isoformat(
        timespec="seconds")
    assert db.get_setting(con, autosend.NEXT_SEND_KEY) == expected_next

    # the second draft waits for the window — same tick never sends two
    result = await autosend.tick()
    assert result["sent"] == 0 and "next send window" in result["reason"]


async def test_profile_without_optin_is_never_picked(
    con, tmp_path, business_hours, sent_messages
):
    _setup_approved(con, tmp_path, auto_send=0)
    _sendable(con)

    result = await autosend.tick()
    assert result == {"sent": 0, "reason": "nothing approved for auto-send"}
    assert sent_messages == []


async def test_inactive_profile_is_never_picked(
    con, tmp_path, business_hours, sent_messages
):
    _setup_approved(con, tmp_path, active=0)
    _sendable(con)

    assert (await autosend.tick())["reason"] == "nothing approved for auto-send"
    assert sent_messages == []


async def test_job_without_profile_is_never_picked(
    con, tmp_path, business_hours, sent_messages
):
    job_id, _ = _setup_approved(con, tmp_path)
    con.execute("UPDATE jobs SET profile_id=NULL WHERE id=?", (job_id,))
    con.commit()
    _sendable(con)

    assert (await autosend.tick())["reason"] == "nothing approved for auto-send"
    assert sent_messages == []


def test_next_approved_autosend_job_requires_all_flags(con):
    assert db.next_approved_autosend_job(con) is None


# -- global blocks ----------------------------------------------------------------
async def test_cap_pauses_queue_without_demoting(
    con, tmp_path, business_hours, sent_messages
):
    job_id, _ = _setup_approved(con, tmp_path)
    _sendable(con)
    db.set_setting(con, "daily_send_cap", "1")
    db.add_email_log(con, {"direction": "outbound_test",
                           "gmail_message_id": "m-x"})
    con.commit()

    result = await autosend.tick()
    assert result == {"sent": 0, "reason": "daily cap reached"}
    assert db.get_draft_by_job(con, job_id)["status"] == "approved"
    assert sent_messages == []


async def test_missing_test_recipient_pauses_queue(
    con, tmp_path, business_hours, sent_messages
):
    job_id, _ = _setup_approved(con, tmp_path)
    config.TOKEN_PATH.write_text("{}", encoding="utf-8")

    result = await autosend.tick()
    assert result["reason"] == "test mode without a test recipient"
    assert db.get_draft_by_job(con, job_id)["status"] == "approved"


async def test_disconnected_gmail_pauses_queue(
    con, tmp_path, business_hours, sent_messages
):
    _setup_approved(con, tmp_path)
    db.set_setting(con, "test_recipient", TEST_INBOX)
    con.commit()

    assert (await autosend.tick())["reason"] == "gmail not connected"
    assert sent_messages == []


# -- failure handling --------------------------------------------------------------
async def test_send_failure_demotes_draft_to_ready_with_reason(
    con, tmp_path, business_hours, monkeypatch
):
    def failing(message):
        raise gmail.GmailError("boom")

    monkeypatch.setattr(gmail, "send_message", failing)
    job_id, _ = _setup_approved(con, tmp_path)
    _sendable(con)

    result = await autosend.tick()
    assert result["sent"] == 0 and "boom" in result["reason"]
    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == "ready"  # out of the pool, needs a human
    assert "auto-send failed" in draft["error"]

    # the failed draft no longer blocks the queue for others
    assert (await autosend.tick())["reason"] == "nothing approved for auto-send"


async def test_real_mode_autosend_records_application(
    con, tmp_path, business_hours, sent_messages
):
    job_id, _ = _setup_approved(con, tmp_path)
    _sendable(con)
    db.set_setting(con, "real_send_enabled", "1")
    con.commit()

    result = await autosend.tick()
    assert result["sent"] == 1 and result["test_mode"] is False
    assert result["recipient"] == "hr@firma.de"
    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == "sent"
    assert db.get_job(con, job_id)["status"] == "applied"
