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
import sqlite3

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
        self.label_calls: list[tuple] = []
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

    def fake_set_labels(message_id, add, remove):
        fake.labeled.append((message_id, add[0] if add else None))
        fake.label_calls.append((message_id, tuple(add), tuple(sorted(remove))))

    monkeypatch.setattr(gmail, "set_labels", fake_set_labels)
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
    # …and it IS marked in Gmail, on both axes: what the mail is, and that
    # it is still waiting for him. Leaving the unsettled mail unlabelled hid
    # exactly the messages that need him.
    _message, add, _remove = inbox.label_calls[0]
    assert set(add) == {"L_JobDeck/Absagen", "L_JobDeck/Zu prüfen"}


async def test_a_mass_mailing_never_closes_an_application_by_itself(
        inbox, con):
    """An HR mailbox sends both kinds of mail. A talent-pool round-robin
    from the very address he corresponded with trips a confident rejection
    pattern, and every other gate passes it: exact address, DMARC, no
    ambiguity. Nobody read his file before sending it, so it may not answer
    for it — and a wrongly filed rank-4 Absage would then block the real
    answer behind it."""
    bewerbung_id = _sent_application(con, email_addr="hr@firma-beispiel.de")
    inbox.add("m-1", subject="Unser Bewerberpool",
              body="Guten Tag,\n\nleider können wir Ihnen derzeit keine "
                   "passende Stelle anbieten.\n\nMit freundlichen Grüßen",
              headers={"list-unsubscribe": "<https://firma-beispiel.de/ab>"})

    outcome = await service.ingest_replies()

    assert outcome["auto_status"] == 0
    assert outcome["review"] == 1
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"
    row = _inbound_rows(con)[0]
    # It is still read and still filed — only the writing is withheld.
    assert (row["matched_by"], row["classification"], row["needs_review"]) \
        == ("address", "absage", 1)


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
    # An out-of-office answers nothing, so the application stays open —
    # which is what "Offen" says. It is still labelled: every mail JobDeck
    # matched carries exactly one JobDeck label.
    assert inbox.labeled == [("m-1", "L_JobDeck/Offen")]


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
def test_a_verdict_files_the_mail_without_reopening_a_closed_application(
        inbox, con):
    """Measured on his real shelf: 23 of 42 waiting mails hang off closed
    applications and 8 propose 'Eingang', so one ordinary press would have
    reopened what he closed himself. The mail is still read — leaving it on
    the shelf would ask the same question again tomorrow — but the register
    is left alone and the screen is told what it kept."""
    bewerbung_id = _sent_application(con)
    db.set_status(con, bewerbung_id, "Einladung", source="user")
    row_id = db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": "m-r",
        "bewerbung_id": bewerbung_id, "needs_review": 1})
    con.commit()

    outcome = service.resolve_review(row_id, "sonstige")

    assert outcome["ok"] is True
    assert outcome["status_written"] is False
    assert (outcome["kept"], outcome["would_be"]) \
        == ("Einladung", "Antwort erhalten")
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Einladung"
    # ... and the mail itself is settled, off the shelf, labelled
    row = db.get_email_log(con, row_id)
    assert (row["classification"], row["classified_by"], row["needs_review"]) \
        == ("sonstige", "reply_manual", 0)


def test_the_second_explicit_press_does_change_the_status(inbox, con):
    """"Stand trotzdem ändern" — his hand, stated twice. Without this the
    guard would be a wall rather than a speed limit, and a genuinely wrong
    Absage could never be talked back."""
    bewerbung_id = _sent_application(con)
    db.set_status(con, bewerbung_id, "Absage", source="user")
    row_id = db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": "m-r",
        "bewerbung_id": bewerbung_id, "needs_review": 1})
    con.commit()

    outcome = service.resolve_review(row_id, "eingang", force_status=True)

    assert (outcome["ok"], outcome["status_written"]) == (True, True)
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "In Bearbeitung"
    # the audit trail says a human did it
    history = con.execute(
        "SELECT source, new_status FROM status_history "
        "WHERE bewerbung_id=? ORDER BY id DESC LIMIT 1",
        (bewerbung_id,)).fetchone()
    assert (history[0], history[1]) == ("reply_manual", "In Bearbeitung")


def test_a_verdict_that_raises_the_status_still_writes_it_on_one_press(
        inbox, con):
    """34 of his 42 waiting mails raise a status, and they must feel exactly
    as they did — the guard is about going backwards, not about slowing the
    ordinary press down."""
    bewerbung_id = _sent_application(con)
    row_id = db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": "m-r",
        "bewerbung_id": bewerbung_id, "needs_review": 1})
    con.commit()

    outcome = service.resolve_review(row_id, "absage")

    assert (outcome["ok"], outcome["status_written"]) == (True, True)
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Absage"


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
    # The row must now say it ATTACHED, not that this app created the
    # ledger row — `undo_receipt` reads exactly this to decide whether an
    # undo may delete a `bewerbungen` row.
    assert db.get_email_log(con, row_id)["matched_by"] == service.MATCHED_ATTACHED


async def test_adopting_onto_a_hand_recorded_application_cannot_be_undone(
        inbox, con):
    """The window the ingestion arm's guard does not cover.

    A receipt whose posting had no application yet is stored as
    MATCHED_RECEIPT and waits on the review pile. He then records the
    application HIMSELF. The press now attaches rather than records — and
    if the row keeps saying MATCHED_RECEIPT, `undo_receipt` accepts and
    `apply_record.undo` deletes the ledger row he wrote by hand."""
    job_id = _strip_job(con)
    row_id = db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": "m-r", "job_id": job_id,
        "matched_by": service.MATCHED_RECEIPT, "classification": "eingang",
        "needs_review": 1})
    # he records it himself, after the mail was already shelved
    bewerbung_id = _sent_application(con)
    db.set_job_status(con, job_id, "applied", bewerbung_id=bewerbung_id)
    con.commit()

    assert service.adopt_receipt(row_id)["ok"] is True

    assert service.undo_receipt(row_id) is False
    assert db.get_bewerbung(con, bewerbung_id) is not None
    assert db.get_job(con, job_id)["bewerbung_id"] == bewerbung_id


async def test_a_job_boards_own_newsletter_cannot_confirm_an_application(
        inbox, con):
    """Found on the first real read of his mailbox: a Jooble job newsletter
    matched a posting whose apply_url IS a jooble.org link — because on a
    board_apply posting that URL is the BOARD's, not the employer's — and
    moved a real application to 'In Bearbeitung'."""
    job_id = _strip_job(
        con, apply_url="https://de.jooble.org/away/4086168173421673246",
        apply_channel="board_apply")
    inbox.add("m-1", from_header="Jooble <subscribe@de.jooble.org>",
              subject="IT-Systemadministrator (w/m/d) bei Beispiel GmbH",
              body="Job-Newsletter 13 August 2026. Ihre Bewerbung ist "
                   "eingegangen.",
              auth=("mx.google.com; spf=pass smtp.mailfrom=de.jooble.org; "
                    "dmarc=pass header.from=de.jooble.org"),
              headers={"list-unsubscribe": "<https://de.jooble.org/unsub>"})

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 0
    assert outcome["ignored"] == 1  # nothing but the opaque id is kept
    assert db.get_job(con, job_id)["bewerbung_id"] is None
    assert _inbound_rows(con) == []


async def test_a_bulk_mailing_can_never_be_a_receipt(inbox, con):
    """Narrower than the auto-reply screen, and deliberately only on the
    receipt arm: a real ATS confirmation may carry an unsubscribe footer,
    but a mailing list may not record an application."""
    job_id = _strip_job(con, apply_url="https://bewerbung.firma-beispiel.de/7")
    inbox.add("m-1", from_header="Firma <karriere@firma-beispiel.de>",
              subject="Eingangsbestätigung",
              body="Ihre Bewerbung ist eingegangen.",
              headers={"list-unsubscribe": "<mailto:u@firma-beispiel.de>"})

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 0
    assert db.get_job(con, job_id)["bewerbung_id"] is None


async def test_a_receipt_proposal_is_marked_as_waiting_in_gmail(inbox, con):
    """The three paths that park a receipt on the review pile each wrote
    the row themselves and none of them labelled, so the proposals were
    invisible in Gmail. One writer does it now."""
    _strip_job(con, company="Musterhaus Softwarebau GmbH")
    inbox.add("m-1",
              from_header="Musterhaus Softwarebau GmbH <hr@musterhaus.example>",
              subject="Ihre Bewerbung ist eingegangen",
              body="Vielen Dank für Ihre Bewerbung.")

    outcome = await service.ingest_replies()

    assert outcome["review"] == 1
    _message, add, _remove = inbox.label_calls[0]
    assert set(add) == {"L_JobDeck/Offen", "L_JobDeck/Zu prüfen"}


async def test_one_bad_message_never_ends_the_pass(inbox, con, monkeypatch):
    """The first real read died on message six of sixty when a concurrent
    pass had already logged one and the UNIQUE id constraint fired. Only
    GmailError was caught; everything else was fatal."""
    bewerbung_id = _sent_application(con, thread="t-9")
    inbox.add("m-bad", thread="t-9", body=ABSAGE_BODY)
    inbox.add("m-good", thread="t-9", body=ABSAGE_BODY)
    original = service._process_message

    def explode(message_id, counters):
        if message_id == "m-bad":
            raise sqlite3.IntegrityError("UNIQUE constraint failed")
        return original(message_id, counters)

    monkeypatch.setattr(service, "_process_message", explode)

    outcome = await service.ingest_replies()

    assert outcome["errors"] == 1
    assert outcome["seen"] == 2  # it kept going
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Absage"
    # the checkpoint is held back so the failed message is retried
    assert db.get_setting(con, service.HISTORY_KEY, "") == ""


async def test_the_board_domain_alone_cannot_authorize_a_receipt(inbox, con):
    """Isolates the board-domain guard from the bulk screen: no unsubscribe
    header, so the ONLY thing that could authorize this write is the
    apply_url domain — which on a board_apply posting belongs to the board."""
    job_id = _strip_job(
        con, apply_url="https://de.jooble.org/away/4086168173421673246",
        apply_channel="board_apply")
    inbox.add("m-1", from_header="Jooble <no-reply@de.jooble.org>",
              subject="Eingangsbestätigung",
              body="Ihre Bewerbung ist eingegangen.",
              auth=("mx.google.com; spf=pass smtp.mailfrom=de.jooble.org; "
                    "dmarc=pass header.from=de.jooble.org"))

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 0
    assert db.get_job(con, job_id)["bewerbung_id"] is None


async def test_the_employers_own_apply_domain_still_authorizes(inbox, con):
    """The guard must not cost the feature: a posting whose apply_url is the
    EMPLOYER's still records from that domain."""
    job_id = _strip_job(con, apply_url="https://bewerbung.firma-beispiel.de/7",
                        apply_channel="company_site")
    inbox.add("m-1", from_header="Firma <karriere@firma-beispiel.de>",
              subject="Eingangsbestätigung",
              body="Ihre Bewerbung ist eingegangen.")

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 1
    assert db.get_job(con, job_id)["bewerbung_id"] is not None


# --------------------------------------------------------------------------
# Gmail labels: every matched mail carries exactly one
# --------------------------------------------------------------------------
async def test_a_waiting_mail_is_labelled_as_waiting(inbox, con):
    """His report: 'not all messages are labelled'. The unsettled ones were
    the only mails with NO label, so in Gmail — on his phone, where the
    labels are the point — the messages needing him were invisible while
    the filed ones were neatly sorted."""
    _sent_application(con, email_addr="info@firma-beispiel.de")
    inbox.add("m-1", body=ABSAGE_BODY,
              from_header="Wer <jemand.anderes@firma-beispiel.de>")

    await service.ingest_replies()

    message, add, remove = inbox.label_calls[0]
    # BOTH axes: what it is, and that it needs him. The verdict label is
    # what he looks for in Gmail; 'Zu prüfen' is what tells him it is not
    # yet filed.
    assert set(add) == {"L_JobDeck/Absagen", "L_JobDeck/Zu prüfen"}
    assert "L_JobDeck/Einladungen" in remove  # nothing else clings on


async def test_a_settled_verdict_takes_the_old_label_off(inbox, con):
    """His second report: 'not all are correctly labelled'. A corrected
    verdict used to add its new label and leave the wrong one in place, so
    one mail could sit under both Absagen and Einladungen."""
    bewerbung_id = _sent_application(con, thread="t-9")
    inbox.add("m-1", thread="t-9", body=ABSAGE_BODY)
    await service.ingest_replies()
    row_id = _inbound_rows(con)[0]["id"]
    inbox.label_calls.clear()

    # Absage and Einladung share rank 4, so this is the deliberate sideways
    # correction — the second, explicit press.
    service.resolve_review(row_id, "einladung", force_status=True)

    message, add, remove = inbox.label_calls[0]
    assert add == ("L_JobDeck/Einladungen",)
    assert "L_JobDeck/Absagen" in remove
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Einladung"


async def test_dismissing_a_mail_strips_its_label(inbox, con):
    """A mail he pushed aside must stop telling him from his phone that
    something is waiting."""
    _sent_application(con, email_addr="info@firma-beispiel.de")
    inbox.add("m-1", body=ABSAGE_BODY,
              from_header="Wer <jemand.anderes@firma-beispiel.de>")
    await service.ingest_replies()
    row_id = _inbound_rows(con)[0]["id"]
    inbox.label_calls.clear()

    service.dismiss_review(row_id)

    message, add, remove = inbox.label_calls[0]
    assert add == ()
    assert set(remove) == {f"L_{name}" for name in service.ALL_LABELS}


async def test_sonstiges_leaves_a_label_behind_rather_than_none(inbox, con):
    """'Sonstiges' is one of the four verdict buttons, and it had no entry in
    LABELS — so pressing it stripped every JobDeck label and applied none.
    In Gmail the mail then looked exactly like mail JobDeck never read, and
    on the correction path a mail correctly filed under Absagen came out
    bare."""
    bewerbung_id = _sent_application(con, thread="t-9")
    inbox.add("m-1", thread="t-9", body=ABSAGE_BODY)
    await service.ingest_replies()
    row_id = _inbound_rows(con)[0]["id"]
    inbox.label_calls.clear()

    assert service.resolve_review(row_id, "sonstige")["ok"] is True

    message, add, remove = inbox.label_calls[0]
    # The label says what happened to the APPLICATION, and 'sonstige' leaves
    # it open — the same thing 'Offen' already means for a receipt.
    assert add == ("L_JobDeck/Offen",)
    assert "L_JobDeck/Absagen" in remove
    # The two axes are independent: the mail is labelled for what it is even
    # though the guard kept the closed status (rank 3 under rank 4).
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Absage"


async def test_adopting_a_receipt_labels_the_mail(inbox, con):
    """Both adoption paths write the register, so both must leave Gmail
    telling the truth. The attach path labelled nothing, so 'Zu prüfen'
    stayed on a mail that was no longer waiting for anything."""
    job_id = _strip_job(con)
    row_id = db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": "m-r", "job_id": job_id,
        "matched_by": service.MATCHED_RECEIPT, "classification": "eingang",
        "needs_review": 1})
    bewerbung_id = _sent_application(con)
    db.set_job_status(con, job_id, "applied", bewerbung_id=bewerbung_id)
    con.commit()
    inbox.label_calls.clear()

    assert service.adopt_receipt(row_id)["ok"] is True

    message, add, remove = inbox.label_calls[0]
    assert (message, add) == ("m-r", ("L_JobDeck/Offen",))
    assert "L_JobDeck/Zu prüfen" in remove


async def test_undoing_a_receipt_puts_the_waiting_label_back(inbox, con):
    """The undo really returns the mail to the review pile, so Gmail has to
    say so again — otherwise his phone shows a settled mail while the app
    shows one waiting for him."""
    job_id = _strip_job(con)
    row_id = db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": "m-r", "job_id": job_id,
        "matched_by": service.MATCHED_RECEIPT, "classification": "eingang",
        "needs_review": 1})
    con.commit()
    assert service.adopt_receipt(row_id)["ok"] is True
    inbox.label_calls.clear()

    assert service.undo_receipt(row_id) is True

    message, add, remove = inbox.label_calls[0]
    assert (message, add) == ("m-r", ("L_JobDeck/Offen",
                                      "L_JobDeck/Zu prüfen"))
    assert db.get_email_log(con, row_id)["needs_review"] == 1
    assert db.get_job(con, job_id)["bewerbung_id"] is None


async def test_unmatched_mail_is_never_labelled(inbox, con):
    """The labels are about HIS applications. Mail that belongs to none of
    them must not be touched in his mailbox at all."""
    inbox.add("m-1", from_header="Fremde <x@anders-beispiel.de>",
              subject="Newsletter", body="Hallo!")

    await service.ingest_replies()

    assert inbox.label_calls == []


async def test_an_invitation_says_so_in_gmail_even_when_it_needs_review(
        inbox, con):
    """His report: 'the most important einladung mail was not identified
    correctly'. It WAS classified as an invitation — but because it matched
    by domain it only carried 'Zu prüfen', so in Gmail it was
    indistinguishable from an unclear receipt. The two facts — what the
    mail is, and whether it needs him — are separate axes."""
    _sent_application(con, email_addr="poststelle@firma-beispiel.de")
    inbox.add("m-1", from_header="Frau Muster <nele.muster@firma-beispiel.de>",
              subject="Ihre Bewerbung um die ausgeschriebene Stelle",
              body=EINLADUNG_BODY)

    await service.ingest_replies()

    row = _inbound_rows(con)[0]
    assert (row["matched_by"], row["classification"], row["needs_review"]) \
        == ("domain", "einladung", 1)
    _message, add, remove = inbox.label_calls[0]
    assert set(add) == {"L_JobDeck/Einladungen", "L_JobDeck/Zu prüfen"}
    assert "L_JobDeck/Offen" in remove


# --------------------------------------------------------------------------
# the company-name arm: reaching a form application
# --------------------------------------------------------------------------
def _form_application(con, *, firma="Firma Beispiel GmbH",
                      status="Gesendet") -> int:
    """A portal application: no address, no thread — the shape 29 of his 55
    open applications have, and the shape every other match arm is blind to."""
    bewerbung_id = db.add_bewerbung(con, {
        "firma": firma, "email": "", "kanal": "Online-Portal",
        "status": status})
    con.commit()
    return bewerbung_id


async def test_a_form_application_is_reachable_by_the_company_name(inbox, con):
    bewerbung_id = _form_application(con)
    inbox.add("m-1", body=ABSAGE_BODY)

    outcome = await service.ingest_replies()

    row = _inbound_rows(con)[0]
    assert row["bewerbung_id"] == bewerbung_id
    assert row["matched_by"] == "name"
    # …and it PROPOSES. A name is a resemblance, not an identification, so it
    # is not in the tier that may file a status.
    assert row["needs_review"] == 1
    assert outcome["auto_status"] == 0
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"


async def test_two_applications_at_one_name_are_refused_not_guessed(inbox, con):
    """Ambiguity is exactly where a guess costs more than the question."""
    _form_application(con)
    _form_application(con)
    inbox.add("m-1", body=ABSAGE_BODY)

    await service.ingest_replies()

    assert _inbound_rows(con) == []


async def test_the_name_arm_prefers_the_application_still_waiting(inbox, con):
    settled = _form_application(con, status="Absage")
    open_one = _form_application(con)
    inbox.add("m-1", body=ABSAGE_BODY)

    await service.ingest_replies()

    row = _inbound_rows(con)[0]
    assert row["bewerbung_id"] == open_one
    assert row["bewerbung_id"] != settled


async def test_an_exact_address_still_beats_the_company_name(inbox, con):
    """The cascade order has to hold: a resemblance must never outrank an
    address he actually wrote to."""
    by_name = _form_application(con)
    by_address = _sent_application(con, email_addr="hr@firma-beispiel.de")
    inbox.add("m-1", body=ABSAGE_BODY)

    await service.ingest_replies()

    row = _inbound_rows(con)[0]
    assert (row["bewerbung_id"], row["matched_by"]) == (by_address, "address")
    assert row["bewerbung_id"] != by_name


async def test_a_mail_with_no_body_is_still_read_from_its_subject(inbox, con):
    """A body goes missing three ways — too large, a fetch failure, or a mail
    that says it all in the header. The subject was thrown away with it, so
    'Absage zu Ihrer Bewerbung' reached the review pile with no label at all,
    which is as plain as German HR mail gets."""
    bewerbung_id = _sent_application(con, email_addr="hr@firma-beispiel.de")
    inbox.add("m-1", subject="Absage zu Ihrer Bewerbung", body="",
              size=service.MAX_RAW_BYTES + 1)

    await service.ingest_replies()

    row = _inbound_rows(con)[0]
    assert row["classification"] == "absage"
    _message, add, _remove = inbox.label_calls[0]
    assert "L_JobDeck/Absagen" in add
    # …and it proposes: one line is thinner evidence than a letter, and the
    # conditional screen has no sentence to work on.
    assert row["needs_review"] == 1
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"
    # the body was never fetched — the size gate ran first
    assert inbox.raw_calls == []


# --------------------------------------------------------------------------
# rescan: making an improvement retroactive
# --------------------------------------------------------------------------
async def test_a_skipped_message_is_read_again_after_a_rescan(inbox, con):
    """A message no application could be found for leaves only its opaque id,
    and that id is what stops the next pass reading it. So every improvement
    to the matching or the German rules reached only mail that had not
    arrived yet — the rescan is what makes one retroactive."""
    inbox.add("m-1", body=ABSAGE_BODY)
    await service.ingest_replies()
    assert _inbound_rows(con) == []          # nothing to match it to, yet

    # he records the application afterwards, then re-arms the reader
    bewerbung_id = _sent_application(con, email_addr="hr@firma-beispiel.de")
    result = service.rescan()
    assert result["forgotten"] == 1

    await service.ingest_replies()

    row = _inbound_rows(con)[0]
    assert row["bewerbung_id"] == bewerbung_id
    assert row["classification"] == "absage"


async def test_a_rescan_never_files_a_matched_message_twice(inbox, con):
    """Only the skipped ids are dropped. A message already tied to an
    application keeps its row, so the duplicate gate still refuses it."""
    _sent_application(con, email_addr="hr@firma-beispiel.de")
    inbox.add("m-1", body=ABSAGE_BODY)
    await service.ingest_replies()
    assert len(_inbound_rows(con)) == 1

    service.rescan()
    await service.ingest_replies()

    assert len(_inbound_rows(con)) == 1
    bewerbung_id = _inbound_rows(con)[0]["bewerbung_id"]
    absagen = [h for h in db.list_status_history(con, bewerbung_id)
               if h["new_status"] == "Absage"]
    assert len(absagen) == 1, "the rejection was filed twice"


async def test_the_rescan_widens_the_window_the_next_full_sync_uses(inbox, con):
    captured = {}

    def fake_list(query, max_results):
        captured["query"] = query
        return []

    import jobdeck.gmail as gmail_mod
    original = gmail_mod.list_new_message_ids
    gmail_mod.list_new_message_ids = fake_list
    try:
        service.rescan(lookback_days=365)
        await service.ingest_replies()
    finally:
        gmail_mod.list_new_message_ids = original

    assert db.get_setting(con, service.LOOKBACK_KEY, "") == "365"
    # a year back, not the thirty days a first run defaults to
    import datetime
    after = int(captured["query"].split("after:")[1])
    days = (datetime.datetime.now()
            - datetime.datetime.fromtimestamp(after)).days
    assert 364 <= days <= 366


def test_an_unparseable_lookback_falls_back_instead_of_crashing(data_dir, con):
    """A settings page is reachable; a crashed scheduler job is not."""
    with db.db() as write:
        db.set_setting(write, service.LOOKBACK_KEY, "sehr lange")
    with db.db() as read:
        assert service._lookback_days(read) == service.FIRST_RUN_LOOKBACK_DAYS
