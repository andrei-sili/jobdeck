"""The ingestion pass end-to-end against a fake Gmail: matching, tiering,
receipts, checkpoints, and the review actions.

Every assertion here is about a WRITE the pass makes (or refuses to make):
statuses through set_status, ledger rows through the one recorder, review
rows, labels, checkpoints. The fake answers exactly the gmail.py functions
the service calls, so the wire shapes stay pinned by test_gmail_read.py and
this file pins the decisions.
"""

import asyncio
import email.message
import email.policy

import pytest

from jobdeck import db, gmail
from jobdeck.ai import llm
from jobdeck.services import replies as service

ABSAGE_BODY = """Sehr geehrter Herr Beispiel,

vielen Dank für Ihre Bewerbung. Nach sorgfältiger Prüfung müssen wir Ihnen
leider mitteilen, dass wir uns für einen anderen Kandidaten entschieden haben.

Mit freundlichen Grüßen"""

EINLADUNG_BODY = """Guten Tag Herr Beispiel,

gerne laden wir Sie zu einem Vorstellungsgespräch ein. Wann hätten Sie Zeit?

Mit freundlichen Grüßen"""

RUECKFRAGE_BODY = """Guten Tag,

könnten Sie uns noch Ihr Abschlusszeugnis nachreichen?

Mit freundlichen Grüßen"""

# DMARC is the only verdict that binds the authenticated identity to the
# From domain — see test_replies.test_only_dmarc_vouches_for_the_from_domain.
AUTH_PASS = ("mx.google.com; spf=pass smtp.mailfrom=firma-beispiel.de; "
             "dmarc=pass header.from=firma-beispiel.de")
# What an attacker sending from their OWN mailbox produces: their domain
# authenticates fine, the forged From does not.
AUTH_FAIL = ("mx.google.com; spf=pass smtp.mailfrom=angreifer.example; "
             "dmarc=fail header.from=firma-beispiel.de")


@pytest.fixture(autouse=True)
def _fresh_lock(monkeypatch):
    monkeypatch.setattr(service, "_lock", asyncio.Lock())


def _raw(body: str) -> bytes:
    message = email.message.EmailMessage(policy=email.policy.default)
    message["From"] = "HR <hr@firma-beispiel.de>"
    message["Subject"] = "Ihre Bewerbung"
    message.set_content(body)
    return message.as_bytes()


class FakeInbox:
    """Answers the gmail.py functions the service calls."""

    def __init__(self):
        self.mails: dict[str, dict] = {}
        self.order: list[str] = []
        self.labeled: list[tuple[str, str]] = []
        self.metadata_calls: list[str] = []
        self.raw_calls: list[str] = []
        self.history_error: Exception | None = None

    def add(self, message_id: str, *, body: str = "",
            from_header: str = "HR <hr@firma-beispiel.de>",
            subject: str = "Ihre Bewerbung", thread: str = "",
            auth: str = AUTH_PASS, size: int | None = None,
            headers: dict | None = None) -> None:
        raw = _raw(body)
        header_map = {"from": from_header, "subject": subject}
        if auth:
            header_map["authentication-results"] = auth
        header_map.update(headers or {})
        self.mails[message_id] = {
            "raw": raw,
            "meta": {
                "id": message_id,
                "thread_id": thread or f"t-{message_id}",
                "snippet": " ".join(body.split())[:100],
                "internal_date_ms": 1755400000000,
                "size_estimate": size if size is not None else len(raw),
                "label_ids": ["INBOX"],
                "headers": header_map,
            },
        }
        self.order.append(message_id)


@pytest.fixture()
def inbox(data_dir, monkeypatch):
    fake = FakeInbox()
    monkeypatch.setattr(gmail, "can_read", lambda: True)
    monkeypatch.setattr(gmail, "profile_history_id", lambda: "h-1")
    monkeypatch.setattr(
        gmail, "list_new_message_ids",
        lambda query, max_results: list(fake.order)[:max_results])

    def fake_history(start, max_results):
        if fake.history_error is not None:
            raise fake.history_error
        return list(fake.order)[:max_results], "h-2"

    monkeypatch.setattr(gmail, "history_added_messages", fake_history)

    def fake_metadata(message_id):
        fake.metadata_calls.append(message_id)
        return dict(fake.mails[message_id]["meta"])

    monkeypatch.setattr(gmail, "get_message_metadata", fake_metadata)

    def fake_raw(message_id):
        fake.raw_calls.append(message_id)
        return fake.mails[message_id]["raw"]

    monkeypatch.setattr(gmail, "get_message_raw", fake_raw)
    monkeypatch.setattr(gmail, "ensure_labels",
                        lambda names: {n: f"L_{n}" for n in names})
    monkeypatch.setattr(gmail, "add_label",
                        lambda message_id, label_id:
                        fake.labeled.append((message_id, label_id)))
    return fake


def _sent_application(con, *, email_addr="hr@firma-beispiel.de",
                      thread="") -> int:
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "email": email_addr,
        "kanal": "E-Mail", "status": "Gesendet"})
    if thread:
        db.add_email_log(con, {"direction": "outbound",
                               "gmail_message_id": f"out-{thread}",
                               "gmail_thread_id": thread,
                               "bewerbung_id": bewerbung_id})
    con.commit()
    return bewerbung_id


def _strip_job(con, *, external_id="j-1", company="Firma Beispiel GmbH",
               **extra) -> int:
    values = {"source": "stub", "external_id": external_id,
              "company": company, "title": "Entwickler",
              "url": f"https://x.example/{external_id}"}
    job_id = db.insert_job_if_new(con, values)
    db.mark_form_opened(con, job_id)
    for column, value in extra.items():
        con.execute(f"UPDATE jobs SET {column}=? WHERE id=?", (value, job_id))
    con.commit()
    return job_id


def _inbound_rows(con):
    return con.execute(
        "SELECT * FROM email_log WHERE direction='inbound' ORDER BY id"
    ).fetchall()


# --------------------------------------------------------------------------
# gates and plumbing
# --------------------------------------------------------------------------
async def test_no_read_permission_notes_the_error_and_stops(
        data_dir, con, monkeypatch):
    monkeypatch.setattr(gmail, "can_read", lambda: False)
    monkeypatch.setattr(gmail, "profile_history_id",
                        lambda: pytest.fail("listed without permission"))
    outcome = await service.ingest_replies()
    assert outcome["error"] == "no read permission"
    assert "Lese-Berechtigung" in db.get_setting(
        con, service.LAST_ERROR_KEY, "")


async def test_a_second_caller_is_told_a_pass_is_running(inbox, con):
    async with service._lock:
        outcome = await service.ingest_replies()
    assert outcome == {"skipped": True}


async def test_history_expiry_falls_back_to_a_full_sync(inbox, con):
    with db.db() as write:
        db.set_setting(write, service.HISTORY_KEY, "h-stale")
    inbox.history_error = gmail.GmailHistoryExpired("expired")
    outcome = await service.ingest_replies()
    assert outcome["errors"] == 0
    # re-baselined on the profile's checkpoint, not the stale one
    assert db.get_setting(con, service.HISTORY_KEY, "") == "h-1"


# --------------------------------------------------------------------------
# the reply path: tiering
# --------------------------------------------------------------------------
async def test_a_thread_matched_rejection_files_itself(inbox, con):
    bewerbung_id = _sent_application(con, thread="t-77")
    inbox.add("m-1", body=ABSAGE_BODY, thread="t-77")

    outcome = await service.ingest_replies()

    assert outcome["auto_status"] == 1
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Absage"
    row = _inbound_rows(con)[0]
    assert (row["matched_by"], row["classification"], row["needs_review"]) \
        == ("thread", "absage", 0)
    assert "anderen Kandidaten" in row["body_text"]
    history = db.list_status_history(con, bewerbung_id)
    assert history[0]["source"] == "reply_auto"
    assert history[0]["email_log_id"] == row["id"]
    assert inbox.labeled == [("m-1", "L_JobDeck/Absagen")]


async def test_an_invitation_by_exact_address_files_itself(inbox, con):
    bewerbung_id = _sent_application(con, email_addr="hr@firma-beispiel.de")
    inbox.add("m-1", body=EINLADUNG_BODY)

    await service.ingest_replies()

    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Einladung"
    row = _inbound_rows(con)[0]
    assert row["matched_by"] == "address"
    assert inbox.labeled == [("m-1", "L_JobDeck/Einladungen")]


async def test_a_domain_match_only_proposes(inbox, con):
    bewerbung_id = _sent_application(con, email_addr="info@firma-beispiel.de")
    inbox.add("m-1", body=ABSAGE_BODY,
              from_header="Frau Muster <andere.person@firma-beispiel.de>")

    outcome = await service.ingest_replies()

    assert outcome["review"] == 1
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"
    row = _inbound_rows(con)[0]
    assert (row["matched_by"], row["classification"], row["needs_review"]) \
        == ("domain", "absage", 1)
    assert inbox.labeled == []


async def test_an_llm_verdict_only_proposes_and_is_metered(
        inbox, con, monkeypatch):
    bewerbung_id = _sent_application(con)
    inbox.add("m-1", body=RUECKFRAGE_BODY)
    with db.db() as write:
        db.set_setting(write, "ai_enabled", "1")
        db.set_setting(write, service.AI_TOGGLE_KEY, "1")
    usage = llm.LLMResult(text="", model="claude-haiku-4-5",
                          input_tokens=80, output_tokens=15, cost_usd=0.0001)
    monkeypatch.setattr(
        service.ai_replies, "classify_reply",
        lambda subject, body: ("sonstige", "Es wird ein Zeugnis erbeten.",
                               usage))

    await service.ingest_replies()

    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"
    row = _inbound_rows(con)[0]
    assert (row["classification"], row["classified_by"], row["needs_review"]) \
        == ("sonstige", "llm", 1)
    assert db.get_setting(con, "llm_calls", "0") == "1"


async def test_the_double_gate_keeps_the_model_silent(inbox, con, monkeypatch):
    _sent_application(con)
    inbox.add("m-1", body=RUECKFRAGE_BODY)
    with db.db() as write:
        db.set_setting(write, "ai_enabled", "1")  # toggle stays off
    monkeypatch.setattr(service.ai_replies, "classify_reply",
                        lambda subject, body:
                        pytest.fail("LLM called through a closed gate"))

    await service.ingest_replies()

    row = _inbound_rows(con)[0]
    assert (row["classification"], row["needs_review"]) == ("", 1)


async def test_an_out_of_office_answers_nothing(inbox, con):
    bewerbung_id = _sent_application(con, thread="t-9")
    inbox.add("m-1", thread="t-9",
              subject="Automatische Antwort: Ihre Bewerbung",
              body="Ich bin bis 25.08. nicht im Hause.")

    outcome = await service.ingest_replies()

    assert outcome["review"] == 0
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"
    row = _inbound_rows(con)[0]
    assert (row["classification"], row["needs_review"]) == ("auto", 0)
    assert inbox.labeled == []


async def test_bulk_headers_settle_what_the_rules_could_not(inbox, con):
    _sent_application(con, thread="t-9")
    inbox.add("m-1", thread="t-9", body="Unser Newsletter im August.",
              headers={"list-unsubscribe": "<mailto:x@y>"})

    outcome = await service.ingest_replies()

    assert outcome["review"] == 0
    row = _inbound_rows(con)[0]
    assert (row["classification"], row["needs_review"]) == ("auto", 0)


async def test_an_oversized_message_never_fetches_its_body(inbox, con):
    _sent_application(con, thread="t-9")
    inbox.add("m-1", thread="t-9", body=ABSAGE_BODY,
              size=service.MAX_RAW_BYTES + 1)

    await service.ingest_replies()

    assert inbox.raw_calls == []
    row = _inbound_rows(con)[0]
    assert (row["body_text"], row["needs_review"]) == ("", 1)


# --------------------------------------------------------------------------
# privacy + idempotency: the opaque-id trace
# --------------------------------------------------------------------------
async def test_unmatched_mail_leaves_only_an_opaque_id(inbox, con):
    inbox.add("m-1", from_header="Fremde <jemand@anders-beispiel.de>",
              subject="Etwas ganz anderes", body="Hallo!")

    await service.ingest_replies()

    row = con.execute("SELECT * FROM email_log").fetchone()
    assert row["direction"] == service.EMAIL_INBOUND_IGNORED
    assert row["gmail_message_id"] == "m-1"
    for column in ("from_addr", "subject", "snippet", "body_text"):
        assert row[column] == "", column
    assert inbox.raw_calls == []  # the body was never even fetched

    await service.ingest_replies()
    assert inbox.metadata_calls == ["m-1"]  # examined exactly once


async def test_his_own_mail_is_ignored(inbox, con):
    with db.db() as write:
        db.set_setting(write, "gmail_address", "Ich@example.com")
    _sent_application(con, email_addr="ich@example.com")
    inbox.add("m-1", from_header="Ich <ich@example.com>", body="Nachfassen")

    outcome = await service.ingest_replies()

    assert outcome["ignored"] == 1
    assert _inbound_rows(con) == []


async def test_the_checkpoint_advances_only_when_drained(
        inbox, con, monkeypatch):
    monkeypatch.setattr(service, "MAX_MESSAGES_PER_PASS", 2)
    for index in range(3):
        inbox.add(f"m-{index}", from_header="X <x@anders-beispiel.de>",
                  body="Hallo")

    await service.ingest_replies()
    assert db.get_setting(con, service.HISTORY_KEY, "") == ""

    await service.ingest_replies()
    assert db.get_setting(con, service.HISTORY_KEY, "") == "h-1"


# --------------------------------------------------------------------------
# receipts against the strip
# --------------------------------------------------------------------------
async def test_a_receipt_from_the_postings_own_domain_records(inbox, con):
    job_id = _strip_job(con, refnr="10000-1177449Z",
                        apply_url="https://bewerbung.firma-beispiel.de/7")
    inbox.add("m-1", from_header="Firma <karriere@firma-beispiel.de>",
              subject="Eingangsbestätigung Referenz 10000-1177449Z",
              body="Ihre Bewerbung ist eingegangen.")

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 1
    job = db.get_job(con, job_id)
    assert job["status"] == "applied"
    bewerbung = db.get_bewerbung(con, job["bewerbung_id"])
    assert bewerbung["status"] == "In Bearbeitung"
    assert bewerbung["kanal"] == "Online-Portal"
    row = _inbound_rows(con)[0]
    assert (row["matched_by"], row["job_id"], row["bewerbung_id"]) \
        == (service.MATCHED_RECEIPT, job_id, job["bewerbung_id"])
    assert inbox.labeled == [("m-1", "L_JobDeck/Offen")]
    history = db.list_status_history(con, job["bewerbung_id"])
    assert history[0]["note"].startswith("Eingangsbestätigung (Absender")


async def test_a_reference_number_alone_cannot_authorize_a_ledger_row(
        inbox, con):
    """The Refnr is printed in the PUBLIC advert: anyone who read the
    posting can quote it, and quoting it says nothing about who sent the
    mail. It may identify which posting a mail is about; it may not
    authorize a write. Reported by the security review with a working
    exploit — a stranger's authenticated mailbox plus a public number was
    enough to spend the one application slot at that company."""
    job_id = _strip_job(con, refnr="10000-1177449Z")
    inbox.add("m-1", from_header="Fremder <wer@voellig-anders.example>",
              subject="Eingangsbestätigung Referenz 10000-1177449Z",
              body="Ihre Bewerbung ist eingegangen.",
              auth=("mx.google.com; spf=pass smtp.mailfrom=voellig-anders."
                    "example; dmarc=pass header.from=voellig-anders.example"))

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 0
    assert db.get_job(con, job_id)["bewerbung_id"] is None
    row = _inbound_rows(con)[0]
    assert (row["needs_review"], row["job_id"]) == (1, job_id)


async def test_a_spoofed_receipt_only_proposes(inbox, con):
    job_id = _strip_job(con, apply_url="https://bewerbung.firma-beispiel.de/7")
    inbox.add("m-1", from_header="Firma <karriere@firma-beispiel.de>",
              subject="Eingangsbestätigung",
              body="Ihre Bewerbung ist eingegangen.", auth=AUTH_FAIL)

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 0
    assert db.get_job(con, job_id)["bewerbung_id"] is None
    row = _inbound_rows(con)[0]
    assert (row["needs_review"], row["job_id"]) == (1, job_id)


async def test_a_receipt_by_sender_domain_records(inbox, con):
    job_id = _strip_job(
        con, apply_url="https://bewerbung.firma-beispiel.de/stelle/7")
    inbox.add("m-1", from_header="Firma <karriere@firma-beispiel.de>",
              subject="Ihre Bewerbung ist eingegangen",
              body="Vielen Dank, wir melden uns.")

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 1
    assert db.get_job(con, job_id)["status"] == "applied"


async def test_two_matching_forms_make_the_receipt_a_proposal(inbox, con):
    first = _strip_job(con, external_id="j-1",
                       apply_url="https://jobs.ats-beispiel.de/a")
    second = _strip_job(con, external_id="j-2", company="Zweite GmbH",
                        apply_url="https://jobs.ats-beispiel.de/b")
    inbox.add("m-1", from_header="ATS <no-reply@ats-beispiel.de>",
              subject="Eingangsbestätigung",
              body="Ihre Bewerbung ist eingegangen.")

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 0
    assert db.get_job(con, first)["bewerbung_id"] is None
    assert db.get_job(con, second)["bewerbung_id"] is None
    assert _inbound_rows(con)[0]["needs_review"] == 1


async def test_a_company_named_receipt_only_proposes(inbox, con):
    job_id = _strip_job(con, company="Musterhaus Softwarebau GmbH")
    inbox.add("m-1",
              from_header="Musterhaus Softwarebau GmbH <hr@musterhaus-beispiel.de>",
              subject="Ihre Bewerbung ist eingegangen",
              body="Vielen Dank für Ihre Bewerbung.")

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 0
    row = _inbound_rows(con)[0]
    assert (row["needs_review"], row["job_id"]) == (1, job_id)


# --------------------------------------------------------------------------
# review actions
# --------------------------------------------------------------------------
def test_his_verdict_always_wins(inbox, con):
    bewerbung_id = _sent_application(con)
    db.set_status(con, bewerbung_id, "Einladung", source="user")
    row_id = db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": "m-r",
        "bewerbung_id": bewerbung_id, "needs_review": 1})
    con.commit()

    assert service.resolve_review(row_id, "sonstige") is True

    # rank 3 under rank 4: an automatic source would have been refused
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Antwort erhalten"
    row = db.get_email_log(con, row_id)
    assert (row["classification"], row["classified_by"], row["needs_review"]) \
        == ("sonstige", "reply_manual", 0)


def test_dismiss_unlinks_and_settles(inbox, con):
    bewerbung_id = _sent_application(con)
    row_id = db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": "m-r",
        "bewerbung_id": bewerbung_id, "needs_review": 1})
    con.commit()

    service.dismiss_review(row_id)

    row = db.get_email_log(con, row_id)
    assert row["bewerbung_id"] is None
    assert row["needs_review"] == 0
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"


def test_adopt_and_undo_a_receipt_roundtrip(inbox, con):
    job_id = _strip_job(con)
    row_id = db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": "m-r",
        "job_id": job_id, "matched_by": "receipt",
        "classification": "eingang", "needs_review": 1})
    con.commit()

    outcome = service.adopt_receipt(row_id)
    assert outcome["ok"] is True
    job = db.get_job(con, job_id)
    assert job["status"] == "applied"
    assert db.get_bewerbung(con, job["bewerbung_id"])["status"] \
        == "In Bearbeitung"
    assert db.get_email_log(con, row_id)["bewerbung_id"] \
        == job["bewerbung_id"]

    assert service.undo_receipt(row_id) is True
    job = db.get_job(con, job_id)
    assert (job["status"], job["bewerbung_id"]) == ("new", None)
    row = db.get_email_log(con, row_id)
    assert (row["needs_review"], row["bewerbung_id"]) == (1, None)
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 0


# --------------------------------------------------------------------------
# the confidence gate at service level
# --------------------------------------------------------------------------
async def test_a_verdict_that_leaned_on_the_screen_never_files_itself(
        inbox, con):
    """The rules read this as a rejection, but only by ranking two families
    against each other — and the identical shape is produced by a receipt
    that merely NAMES a possible rejection. A thread match is not enough:
    an unconfident verdict is a proposal wherever it came from."""
    bewerbung_id = _sent_application(con, thread="t-9")
    inbox.add("m-1", thread="t-9", body=(
        "Sehr geehrter Herr Beispiel,\n\nIhre Bewerbung ist bei uns "
        "eingegangen. Wir müssen Ihnen mitteilen, dass wir Sie nicht weiter "
        "berücksichtigen können.\n\nMit freundlichen Grüßen"))

    outcome = await service.ingest_replies()

    assert outcome["auto_status"] == 0
    assert outcome["review"] == 1
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"
    row = _inbound_rows(con)[0]
    assert (row["classification"], row["needs_review"]) == ("absage", 1)


async def test_an_unauthenticated_sender_cannot_file_a_status_by_address(
        inbox, con):
    """The address arm matches on the From header, which is what a forger
    writes. Without Gmail's own DMARC verdict the rules may propose, never
    file. (A thread match needs no such check — an attacker cannot forge
    the threadId Gmail assigns to a message this app sent.)"""
    bewerbung_id = _sent_application(con, email_addr="hr@firma-beispiel.de")
    inbox.add("m-1", body=ABSAGE_BODY, auth=AUTH_FAIL)

    outcome = await service.ingest_replies()

    assert outcome["auto_status"] == 0
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"
    assert _inbound_rows(con)[0]["needs_review"] == 1


async def test_a_receipt_attached_to_a_hand_recorded_application_offers_no_undo(
        inbox, con):
    """The healing arm attaches a receipt to an application HE recorded.
    Undoing there would delete a ledger row this app never wrote."""
    job_id = _strip_job(con, apply_url="https://bewerbung.firma-beispiel.de/7")
    bewerbung_id = _sent_application(con)
    db.set_job_status(con, job_id, "applied", bewerbung_id=bewerbung_id)
    con.commit()
    inbox.add("m-1", from_header="Firma <karriere@firma-beispiel.de>",
              subject="Eingangsbestätigung",
              body="Ihre Bewerbung ist eingegangen.")

    await service.ingest_replies()

    row = _inbound_rows(con)[0]
    assert row["matched_by"] == service.MATCHED_ATTACHED
    assert service.undo_receipt(row["id"]) is False
    # the application he recorded is still there
    assert db.get_bewerbung(con, bewerbung_id) is not None
    assert db.get_job(con, job_id)["bewerbung_id"] == bewerbung_id


async def test_the_backlog_is_read_oldest_first(inbox, con):
    """A search answers newest-first. Reading in that order let an OLDER
    mail be processed after a newer one, so the earlier word became the
    last one written."""
    bewerbung_id = _sent_application(con, thread="t-9")
    # the inbox lists newest first: the receipt is the OLDER mail
    inbox.add("m-neu", thread="t-9", body=ABSAGE_BODY)
    inbox.add("m-alt", thread="t-9",
              body="Guten Tag,\n\nIhre Bewerbung ist bei uns eingegangen.")

    await service.ingest_replies()

    assert [row["gmail_message_id"] for row in _inbound_rows(con)] \
        == ["m-alt", "m-neu"]
    # read in arrival order the receipt lands first and the rejection last
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Absage"


async def test_a_refused_automatic_write_asks_him_instead_of_going_quiet(
        inbox, con):
    """Two settled verdicts in one backlog: the second cannot be applied
    automatically (no automatic source moves a verdict sideways), and
    leaving it filed would put 'Absage · automatisch' in the ledger beside
    an application reading Einladung."""
    bewerbung_id = _sent_application(con, thread="t-9")
    inbox.add("m-2", thread="t-9", body=ABSAGE_BODY)
    inbox.add("m-1", thread="t-9", body=EINLADUNG_BODY)

    await service.ingest_replies()

    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Einladung"
    rows = {row["gmail_message_id"]: row for row in _inbound_rows(con)}
    assert rows["m-1"]["needs_review"] == 0
    assert rows["m-2"]["needs_review"] == 1  # the rejection waits for him
    assert rows["m-2"]["classification"] == "absage"


async def test_a_form_applications_later_answer_finds_its_application(
        inbox, con):
    """A form application sends nothing, so its thread's only anchor is the
    receipt already read into it. Consulting outbound rows alone left every
    later answer — the real Absage or Einladung — unmatched and dropped;
    roughly half his applications go out that way."""
    job_id = _strip_job(con)
    bewerbung_id = db.add_bewerbung(con, {"firma": "Firma Beispiel GmbH",
                                          "email": "", "kanal": "Online-Portal",
                                          "status": "Gesendet"})
    db.set_job_status(con, job_id, "applied", bewerbung_id=bewerbung_id)
    db.add_email_log(con, {"direction": "inbound", "gmail_message_id": "m-0",
                           "gmail_thread_id": "t-form",
                           "bewerbung_id": bewerbung_id,
                           "matched_by": service.MATCHED_RECEIPT,
                           "classification": "eingang"})
    con.commit()
    inbox.add("m-1", thread="t-form", body=ABSAGE_BODY)

    await service.ingest_replies()

    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Absage"
    row = [r for r in _inbound_rows(con) if r["gmail_message_id"] == "m-1"][0]
    assert (row["matched_by"], row["bewerbung_id"]) == ("thread", bewerbung_id)


async def test_adopting_a_receipt_for_an_already_recorded_job_attaches(
        inbox, con):
    """Recording twice makes `apply_job` mark the posting a DUPLICATE of its
    own application. The press means 'this mail belongs to that
    application', so it attaches."""
    job_id = _strip_job(con)
    bewerbung_id = _sent_application(con)
    db.set_job_status(con, job_id, "applied", bewerbung_id=bewerbung_id)
    row_id = db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": "m-r", "job_id": job_id,
        "matched_by": service.MATCHED_RECEIPT, "classification": "eingang",
        "needs_review": 1})
    con.commit()

    outcome = service.adopt_receipt(row_id)

    assert outcome["ok"] is True
    job = db.get_job(con, job_id)
    assert (job["status"], job["bewerbung_id"]) == ("applied", bewerbung_id)
    assert db.get_email_log(con, row_id)["bewerbung_id"] == bewerbung_id
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 1
