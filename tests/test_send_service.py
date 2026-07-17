import asyncio
import datetime
import time

import pytest

from jobdeck import config, db, gmail
from jobdeck.services import send

TEST_INBOX = "inbox@test.example"


# -- fixtures / helpers --------------------------------------------------------
@pytest.fixture(autouse=True)
def _fresh_lock(monkeypatch):
    monkeypatch.setattr(send, "_lock", asyncio.Lock())


@pytest.fixture()
def sent_messages(monkeypatch):
    """Stub the Gmail seam; returns the captured MIME messages."""
    calls = []

    def fake_send(message):
        calls.append(message)
        return (f"m-{len(calls)}", f"t-{len(calls)}")

    monkeypatch.setattr(gmail, "send_message", fake_send)
    return calls


@pytest.fixture()
def gmail_connected(data_dir):
    config.TOKEN_PATH.write_text("{}", encoding="utf-8")


def _must_not_send(message):
    raise AssertionError("gmail send called although a gate should have fired")


def _insert_job(con, **over):
    values = dict(
        source="stub", external_id=over.pop("external_id", "j1"),
        title="Python Dev", company="Firma GmbH", description="desc",
        contact_email="hr@firma.de", url="https://example.org/job",
    )
    values.update(over)
    job_id = db.insert_job_if_new(con, values)
    db.set_job_contacts(con, job_id, {
        "ansprechpartner": "Frau Weber", "contact_strasse": "Weg 1",
        "contact_plz_ort": "10115 Berlin",
    })
    return job_id


def _pdf(tmp_path, name="Bewerbung_Max_Muster_Firma_GmbH.pdf"):
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4 fake")
    return path


def _ready_draft(con, job_id, pdf_path="", **over):
    values = dict(
        status="ready", recipient="hr@firma.de",
        betreff="Bewerbung als Python Dev, K-17 – Max Muster",
        email_body="Guten Tag,\n\nanbei meine Bewerbung.\n\n"
                   "Mit freundlichen Grüßen\nMax Muster",
        anschreiben_body="Sehr geehrte Frau Weber,\n\nAbsatz.\n\n"
                         "Mit freundlichen Grüßen\nMax Muster",
        pdf_path=str(pdf_path) if pdf_path else "",
    )
    values.update(over)
    draft_id = db.upsert_draft(con, job_id, values)
    con.commit()
    return draft_id


def _settings(con, **kv):
    for key, value in kv.items():
        db.set_setting(con, key, value)
    con.commit()


# -- recipient resolution (the fail-closed core) -------------------------------
def test_resolve_recipient_test_mode_requires_configured_inbox():
    settings = {"real_send_enabled": "0", "test_recipient": ""}
    recipient, test_mode, error = send.resolve_recipient("hr@firma.de", settings)
    assert recipient == "" and test_mode is True
    assert "no test recipient" in error


def test_resolve_recipient_test_mode_overrides_draft_recipient():
    settings = {"real_send_enabled": "0", "test_recipient": TEST_INBOX}
    recipient, test_mode, error = send.resolve_recipient("hr@firma.de", settings)
    assert (recipient, test_mode, error) == (TEST_INBOX, True, "")


def test_resolve_recipient_real_mode_uses_draft_recipient():
    settings = {"real_send_enabled": "1", "test_recipient": TEST_INBOX}
    recipient, test_mode, error = send.resolve_recipient(" hr@firma.de ", settings)
    assert (recipient, test_mode, error) == ("hr@firma.de", False, "")


# -- gate chain ----------------------------------------------------------------
async def test_gates_fire_in_order_without_any_send(
    con, data_dir, tmp_path, monkeypatch
):
    monkeypatch.setattr(gmail, "send_message", _must_not_send)

    result = await send.send_draft(999)
    assert not result["ok"] and "posting not found" in result["error"]

    job_id = _insert_job(con)
    con.commit()
    result = await send.send_draft(job_id)
    assert not result["ok"] and "draft the application first" in result["error"]

    db.upsert_draft(con, job_id, {"status": "generating"})
    con.commit()
    result = await send.send_draft(job_id)
    assert not result["ok"] and "not sendable" in result["error"]

    _ready_draft(con, job_id, betreff="")
    result = await send.send_draft(job_id)
    assert not result["ok"] and "no Betreff" in result["error"]

    _ready_draft(con, job_id, pdf_path="")
    result = await send.send_draft(job_id)
    assert not result["ok"] and "Bewerbungsmappe" in result["error"]

    _ready_draft(con, job_id, pdf_path=tmp_path / "gone.pdf")
    result = await send.send_draft(job_id)
    assert not result["ok"] and "gone" in result["error"]

    pdf = _pdf(tmp_path)
    _ready_draft(con, job_id, pdf_path=pdf)
    result = await send.send_draft(job_id)  # real OFF, no test recipient
    assert not result["ok"] and "no test recipient" in result["error"]

    _settings(con, test_recipient="not-an-address")
    result = await send.send_draft(job_id)
    assert not result["ok"] and "valid e-mail address" in result["error"]

    _settings(con, test_recipient=TEST_INBOX, daily_send_cap="0")
    result = await send.send_draft(job_id)
    assert not result["ok"] and "cap reached" in result["error"]

    _settings(con, daily_send_cap="15")
    result = await send.send_draft(job_id)  # no token.json yet
    assert not result["ok"] and "Connect Gmail" in result["error"]

    # nothing ever left, nothing was recorded
    assert con.execute("SELECT COUNT(*) FROM email_log").fetchone()[0] == 0
    assert db.get_draft_by_job(con, job_id)["status"] == "ready"


# -- test mode -----------------------------------------------------------------
async def test_test_send_goes_to_test_inbox_and_consumes_nothing(
    con, gmail_connected, sent_messages, tmp_path
):
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    _settings(con, test_recipient=TEST_INBOX, applicant_name="Max Muster",
              gmail_address="max@gmail.com")

    result = await send.send_draft(job_id)
    assert result["ok"], result["error"]
    assert result["test_mode"] is True
    assert result["recipient"] == TEST_INBOX

    # the wire message went to the test inbox, not the company
    assert len(sent_messages) == 1
    assert sent_messages[0]["To"] == TEST_INBOX
    assert str(sent_messages[0]["From"]) == "Max Muster <max@gmail.com>"

    # audit row exists, but draft/job/applications are untouched
    log_row = con.execute("SELECT * FROM email_log").fetchone()
    assert log_row["direction"] == "outbound_test"
    assert log_row["to_addr"] == TEST_INBOX
    assert log_row["gmail_message_id"] == "m-1"
    assert log_row["bewerbung_id"] is None
    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == "ready"
    assert draft["gmail_message_id"] == ""
    assert db.get_job(con, job_id)["status"] == "new"
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 0


async def test_test_send_is_repeatable(con, gmail_connected, sent_messages,
                                        tmp_path):
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    _settings(con, test_recipient=TEST_INBOX)

    assert (await send.send_draft(job_id))["ok"]
    assert (await send.send_draft(job_id))["ok"]
    assert len(sent_messages) == 2
    assert db.count_outbound_today(con) == 2


# -- real mode -----------------------------------------------------------------
async def test_real_send_records_application_log_and_draft(
    con, gmail_connected, sent_messages, tmp_path
):
    job_id = _insert_job(con)
    pdf = _pdf(tmp_path)
    _ready_draft(con, job_id, pdf_path=pdf)
    _settings(con, real_send_enabled="1", applicant_name="Max Muster",
              gmail_address="max@gmail.com")

    result = await send.send_draft(job_id)
    assert result["ok"], result["error"]
    assert result["test_mode"] is False
    assert result["recipient"] == "hr@firma.de"
    assert sent_messages[0]["To"] == "hr@firma.de"

    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == "sent"
    assert draft["gmail_message_id"] == "m-1"
    assert draft["gmail_thread_id"] == "t-1"

    bewerbung = db.get_bewerbung(con, draft["bewerbung_id"])
    assert bewerbung["firma"] == "Firma GmbH"
    assert bewerbung["email"] == "hr@firma.de"
    assert bewerbung["kanal"] == "E-Mail"
    assert bewerbung["status"] == "Gesendet"
    assert bewerbung["dokument"] == str(pdf)
    assert bewerbung["gesendet_am"] == datetime.date.today().isoformat()
    assert bewerbung["ansprechpartner"] == "Frau Weber"

    job = db.get_job(con, job_id)
    assert job["status"] == "applied"
    assert job["bewerbung_id"] == bewerbung["id"]

    log_row = con.execute("SELECT * FROM email_log").fetchone()
    assert log_row["direction"] == "outbound"
    assert log_row["draft_id"] == draft["id"]
    assert log_row["bewerbung_id"] == bewerbung["id"]

    history = db.list_status_history(con, bewerbung["id"])
    assert history and history[0]["new_status"] == "Gesendet"


async def test_real_send_requires_plausible_recipient(
    con, gmail_connected, tmp_path, monkeypatch
):
    monkeypatch.setattr(gmail, "send_message", _must_not_send)
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path), recipient="")
    _settings(con, real_send_enabled="1")

    result = await send.send_draft(job_id)
    assert not result["ok"] and "valid e-mail address" in result["error"]


async def test_duplicate_company_blocks_real_send_only(
    con, gmail_connected, sent_messages, tmp_path
):
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    db.add_bewerbung(con, {"firma": "Firma GmbH", "email": "hr@firma.de",
                           "status": "Gesendet"})
    con.commit()

    _settings(con, real_send_enabled="1")
    result = await send.send_draft(job_id)
    assert not result["ok"] and "already applied" in result["error"]

    _settings(con, real_send_enabled="0", test_recipient=TEST_INBOX)
    result = await send.send_draft(job_id)  # test sends don't care
    assert result["ok"], result["error"]


async def test_already_recorded_send_blocks_a_second_real_send(
    con, gmail_connected, tmp_path, monkeypatch
):
    monkeypatch.setattr(gmail, "send_message", _must_not_send)
    job_id = _insert_job(con)
    draft_id = _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    db.add_email_log(con, {"direction": "outbound", "draft_id": draft_id,
                           "gmail_message_id": "m-old"})
    con.commit()
    _settings(con, real_send_enabled="1")

    result = await send.send_draft(job_id)
    assert not result["ok"] and "already has a recorded send" in result["error"]


# -- daily cap -----------------------------------------------------------------
async def test_daily_cap_counts_test_and_real_sends(
    con, gmail_connected, tmp_path, monkeypatch
):
    monkeypatch.setattr(gmail, "send_message", _must_not_send)
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    db.add_email_log(con, {"direction": "outbound", "gmail_message_id": "m-a"})
    db.add_email_log(con, {"direction": "outbound_test",
                           "gmail_message_id": "m-b"})
    con.commit()
    assert db.count_outbound_today(con) == 2

    _settings(con, test_recipient=TEST_INBOX, daily_send_cap="2")
    result = await send.send_draft(job_id)
    assert not result["ok"] and "cap reached (2/2)" in result["error"]


def test_count_outbound_today_ignores_other_days_and_inbound(con):
    db.add_email_log(con, {"direction": "outbound", "gmail_message_id": "m-1"})
    db.add_email_log(con, {"direction": "inbound", "gmail_message_id": "m-2"})
    con.execute("UPDATE email_log SET created_at='2020-01-01T09:00:00' "
                "WHERE gmail_message_id='m-1'")
    con.commit()
    assert db.count_outbound_today(con) == 0


# -- claim / double-send protection ---------------------------------------------
async def test_sending_status_blocks_and_is_never_auto_reclaimed(
    con, gmail_connected, tmp_path, monkeypatch
):
    monkeypatch.setattr(gmail, "send_message", _must_not_send)
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path), status="sending")
    con.execute("UPDATE drafts SET updated_at='2020-01-01T00:00:00'")
    con.commit()  # ancient claim: drafting would reclaim — sending must NOT
    _settings(con, test_recipient=TEST_INBOX)

    result = await send.send_draft(job_id)
    assert not result["ok"] and "already in progress" in result["error"]
    assert db.get_draft_by_job(con, job_id)["status"] == "sending"


async def test_concurrent_real_sends_send_exactly_once(
    con, gmail_connected, tmp_path, monkeypatch
):
    calls = []

    def slow_send(message):
        time.sleep(0.05)
        calls.append(message)
        return ("m-1", "t-1")

    monkeypatch.setattr(gmail, "send_message", slow_send)
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    _settings(con, real_send_enabled="1")

    first, second = await asyncio.gather(
        send.send_draft(job_id), send.send_draft(job_id)
    )
    assert sorted([first["ok"], second["ok"]]) == [False, True]
    failed = first if not first["ok"] else second
    assert "already" in failed["error"]
    assert len(calls) == 1
    assert con.execute("SELECT COUNT(*) FROM email_log").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 1


def test_claim_refuses_when_draft_changed_after_snapshot(con, data_dir,
                                                         tmp_path):
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    snapshot = dict(db.get_draft_by_job(con, job_id))
    snapshot["betreff"] = "what the user THOUGHT they approved"

    error = send._claim(job_id, snapshot, test_mode=True)
    assert "changed while preparing" in error
    assert db.get_draft_by_job(con, job_id)["status"] == "ready"


# -- expectation pinning (what the human approved is what leaves) --------------
async def test_content_edit_after_the_confirmation_refuses_the_send(
    con, gmail_connected, tmp_path, monkeypatch
):
    monkeypatch.setattr(gmail, "send_message", _must_not_send)
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    _settings(con, test_recipient=TEST_INBOX)
    shown = dict(db.get_draft_by_job(con, job_id))

    # the user edits the draft in another tab after confirming
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path),
                 recipient="someone-else@firma.de")

    result = await send.send_draft(job_id, expect={
        "updated_at": shown["updated_at"], "recipient": shown["recipient"],
        "betreff": shown["betreff"], "email_body": shown["email_body"],
    })
    assert not result["ok"] and "changed since you reviewed" in result["error"]


async def test_mode_flip_after_the_confirmation_refuses_the_send(
    con, gmail_connected, tmp_path, monkeypatch
):
    """The dialog said TEST; real sending was switched on meanwhile."""
    monkeypatch.setattr(gmail, "send_message", _must_not_send)
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    _settings(con, test_recipient=TEST_INBOX, real_send_enabled="1")

    result = await send.send_draft(job_id, expect={"test_mode": True})
    assert not result["ok"] and "sending mode changed" in result["error"]

    result = await send.send_draft(job_id, expect={
        "recipient_shown": TEST_INBOX})
    assert not result["ok"] and "recipient changed" in result["error"]


async def test_unapproved_draft_is_not_auto_sent(
    con, gmail_connected, tmp_path, monkeypatch
):
    """Auto-send picked it as approved; the user un-approved in the window."""
    monkeypatch.setattr(gmail, "send_message", _must_not_send)
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path), status="ready")
    _settings(con, test_recipient=TEST_INBOX)

    result = await send.send_draft(job_id, expect={"status": "approved"})
    assert not result["ok"] and "no longer approved" in result["error"]
    assert db.get_draft_by_job(con, job_id)["status"] == "ready"


async def test_matching_expectation_sends(con, gmail_connected, sent_messages,
                                          tmp_path):
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    _settings(con, test_recipient=TEST_INBOX)
    shown = dict(db.get_draft_by_job(con, job_id))

    result = await send.send_draft(job_id, expect={
        "updated_at": shown["updated_at"], "betreff": shown["betreff"],
        "test_mode": True, "recipient_shown": TEST_INBOX, "status": "ready",
    })
    assert result["ok"], result["error"]
    assert len(sent_messages) == 1


# -- failure paths --------------------------------------------------------------
async def test_uncertain_transport_failure_keeps_the_claim(
    con, gmail_connected, tmp_path, monkeypatch
):
    """A lost response may mean the mail DID go out — never invite a retry."""
    def uncertain(message):
        raise gmail.GmailUncertain("could not reach Gmail: timed out")

    monkeypatch.setattr(gmail, "send_message", uncertain)
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    _settings(con, real_send_enabled="1")

    result = await send.send_draft(job_id)
    assert not result["ok"] and "may or may not have gone out" in result["error"]
    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == "sending"  # stays for human resolution
    assert "outcome unknown" in draft["error"]


async def test_refused_send_releases_the_claim(
    con, gmail_connected, tmp_path, monkeypatch
):
    """Gmail answered: definitively not sent — the user may retry freely."""
    def refused(message):
        raise gmail.GmailRefused("Gmail refused the send: invalidArgument")

    monkeypatch.setattr(gmail, "send_message", refused)
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    _settings(con, real_send_enabled="1")

    result = await send.send_draft(job_id)
    assert not result["ok"] and "invalidArgument" in result["error"]
    assert db.get_draft_by_job(con, job_id)["status"] == "ready"


async def test_build_failure_after_the_claim_releases_it(
    con, gmail_connected, tmp_path, monkeypatch
):
    """The Mappe vanishing between the gate and the read must not strand."""
    def broken(**kwargs):
        raise OSError("Mappe disappeared under us")

    monkeypatch.setattr(gmail, "build_mime", broken)
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path), status="approved")
    _settings(con, test_recipient=TEST_INBOX)

    with pytest.raises(OSError):
        await send.send_draft(job_id)
    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == "approved"  # released, not stranded
    assert "unexpectedly" in draft["error"]


async def test_manual_resolution_during_a_send_is_not_double_recorded(
    con, gmail_connected, tmp_path, monkeypatch
):
    """The human recorded the send while it was in flight — one record only."""
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    _settings(con, real_send_enabled="1")

    def resolve_mid_flight(message):
        send.resolve_sending(job_id, assume_sent=True)
        return ("m-real", "t-real")

    monkeypatch.setattr(gmail, "send_message", resolve_mid_flight)

    result = await send.send_draft(job_id)
    assert result["ok"]
    assert "already recorded manually" in result["error"]
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM email_log").fetchone()[0] == 1
    # the ids the human could not know are backfilled onto their record
    assert db.get_draft_by_job(con, job_id)["gmail_message_id"] == "m-real"
    assert con.execute(
        "SELECT gmail_message_id FROM email_log").fetchone()[0] == "m-real"



async def test_send_failure_releases_claim_with_error(
    con, gmail_connected, tmp_path, monkeypatch
):
    def failing(message):
        raise gmail.GmailError("Gmail refused the send: quotaExceeded")

    monkeypatch.setattr(gmail, "send_message", failing)
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path), status="approved")
    _settings(con, test_recipient=TEST_INBOX)

    result = await send.send_draft(job_id)
    assert not result["ok"] and "quotaExceeded" in result["error"]
    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == "approved"  # released to what it was
    assert "quotaExceeded" in draft["error"]
    assert con.execute("SELECT COUNT(*) FROM email_log").fetchone()[0] == 0


async def test_recording_failure_leaves_sending_and_raises(
    con, gmail_connected, sent_messages, tmp_path, monkeypatch
):
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    _settings(con, test_recipient=TEST_INBOX)

    def broken(con_, values):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "add_email_log", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        await send.send_draft(job_id)
    # the mail left, the books are wrong: stay in 'sending' for the human
    assert db.get_draft_by_job(con, job_id)["status"] == "sending"


# -- queue transitions -----------------------------------------------------------
def test_approve_requires_ready_content_and_pdf(con, data_dir, tmp_path):
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path="")
    assert "Bewerbungsmappe" in send.approve(job_id)["error"]

    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    result = send.approve(job_id)
    assert result["ok"] and result["draft"]["status"] == "approved"

    assert "cannot move" in send.approve(job_id)["error"]  # already approved

    result = send.unapprove(job_id)
    assert result["ok"] and result["draft"]["status"] == "ready"


def test_discard_and_restore(con, data_dir, tmp_path):
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    assert send.discard(job_id)["draft"]["status"] == "discarded"
    assert send.restore(job_id)["draft"]["status"] == "ready"

    db.upsert_draft(con, job_id, {"status": "sent"})
    con.commit()
    assert "cannot move" in send.discard(job_id)["error"]  # sent is immutable


def test_resolve_sending_not_sent_returns_to_ready(con, data_dir, tmp_path):
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path), status="sending")

    result = send.resolve_sending(job_id, assume_sent=False)
    assert result["ok"] and result["draft"]["status"] == "ready"
    assert "assumed not sent" in result["draft"]["error"]
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 0


def test_resolve_sending_assume_sent_records_without_gmail_ids(
    con, data_dir, tmp_path
):
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path), status="sending")

    result = send.resolve_sending(job_id, assume_sent=True)
    assert result["ok"], result["error"]
    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == "sent" and draft["gmail_message_id"] == ""
    log_row = con.execute("SELECT * FROM email_log").fetchone()
    assert log_row["gmail_message_id"] is None
    assert log_row["matched_by"] == "manual_resolution"
    assert db.get_job(con, job_id)["status"] == "applied"
    assert db.get_bewerbung(con, draft["bewerbung_id"])["status"] == "Gesendet"


def test_two_manual_resolutions_do_not_collide_on_unique_message_id(
    con, data_dir, tmp_path
):
    for external_id in ("j1", "j2"):
        job_id = _insert_job(con, external_id=external_id)
        _ready_draft(con, job_id, pdf_path=_pdf(tmp_path), status="sending")
        assert send.resolve_sending(job_id, assume_sent=True)["ok"]
    assert con.execute("SELECT COUNT(*) FROM email_log").fetchone()[0] == 2


def test_resolve_sending_requires_sending_status(con, data_dir, tmp_path):
    job_id = _insert_job(con)
    _ready_draft(con, job_id, pdf_path=_pdf(tmp_path))
    assert "nothing to resolve" in send.resolve_sending(
        job_id, assume_sent=True
    )["error"]
