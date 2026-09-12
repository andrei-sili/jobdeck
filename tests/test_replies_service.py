"""The ingestion pass end-to-end against a fake Gmail: matching, tiering,
receipts, checkpoints, and the review actions.

Every assertion here is about a WRITE the pass makes (or refuses to make):
statuses through set_status, ledger rows through the one recorder, review
rows, labels, checkpoints. The fake answers exactly the gmail.py functions
the service calls, so the wire shapes stay pinned by test_gmail_read.py and
this file pins the decisions.
"""

import asyncio
import datetime
import email.message
import email.policy
import sqlite3

import pytest

from jobdeck import db, gmail, replies
from jobdeck.ai import llm
from jobdeck.constants import FORM_OPENED_UNKNOWN
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


def _now_ms() -> int:
    return int(datetime.datetime.now().timestamp() * 1000)


def _ms(stamp: str) -> int:
    """A local naive ISO stamp as Gmail's internalDate — the same frame the
    service reads it back in (`_iso_from_ms` uses fromtimestamp)."""
    return int(datetime.datetime.fromisoformat(stamp).timestamp() * 1000)


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
            headers: dict | None = None,
            internal_date_ms: int | None = None) -> None:
        """A mail arrives NOW unless a test says otherwise.

        The default used to be a fixed moment in the past, which quietly
        made every receipt fixture older than the form it confirmed — the
        exact shape `_follows_the_opening` refuses. A stub that cannot
        happen cannot guard anything."""
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
                "internal_date_ms": (
                    _now_ms() if internal_date_ms is None else internal_date_ms),
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


# a multi-tenant ATS domain names nobody
VENDOR_AUTH = ("mx.google.com; spf=pass smtp.mailfrom=join.com; "
               "dmarc=pass header.from=join.com")


async def test_a_vendor_receipt_naming_another_employer_is_not_this_postings_mail(
        inbox, con):
    """`join.com` is the apply_url of EVERY posting applied to through JOIN,
    so the domain aligned with all of them at once. On his mailbox fifteen
    JOIN receipts — each naming its own employer in its own subject — were
    identified as one posting at a sixteenth company, and only the guard
    that a receipt cannot predate its form kept them from writing a status.

    Refused outright rather than proposed: the receipt arm runs before the
    name arm, so declining here is what gives the mail its chance at the
    application it really belongs to."""
    job_id = _strip_job(con, apply_url="https://join.com/companies/x/jobs/7")
    inbox.add("m-1", from_header="JOIN <noreply@join.com>",
              subject="Deine Bewerbung bei Anders Software",
              body="Wir haben deine Bewerbung erhalten.", auth=VENDOR_AUTH)

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 0
    assert db.get_job(con, job_id)["bewerbung_id"] is None
    # not even a proposal: nothing here is about this posting
    assert _inbound_rows(con) == []


async def test_a_vendor_receipt_that_names_this_employer_still_records(
        inbox, con):
    """The other half of the guard, and the reason it is not a blanket refusal.

    The employer has to be named where the VENDOR writes it — its display name
    here, its tenant slot on a Personio-style address — because that is the
    part of the envelope a sender cannot fake by being itself. The mail's own
    words are not enough: a company key is its name without the legal form, so a
    one-word name keys to an ordinary word, and a genuine receipt for a
    different employer recorded an application at a company it never
    mentioned."""
    job_id = _strip_job(con, apply_url="https://join.com/companies/x/jobs/7")
    inbox.add("m-1", from_header="Firma Beispiel GmbH <noreply@join.com>",
              subject="Deine Bewerbung",
              body="Ihre Bewerbung ist eingegangen.", auth=VENDOR_AUTH)

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 1
    job = db.get_job(con, job_id)
    assert job["bewerbung_id"] is not None
    assert db.get_bewerbung(con, job["bewerbung_id"])["status"] \
        == "In Bearbeitung"


async def test_a_vendor_receipt_naming_the_employer_only_in_its_words_proposes(
        inbox, con):
    """The security review's second reproduction. A genuine vendor receipt for
    ANOTHER employer, whose text happens to contain this posting's company as a
    word, recorded an application at a company the mail never wrote about — a
    key is a name without its legal form, so a one-word company name keys to an
    ordinary word of the language. A length floor cannot tell a name from a
    word, so the authorizing gate stopped reading the mail's words at all."""
    job_id = _strip_job(con, company="Leuchte GmbH",
                        apply_url="https://join.com/companies/x/jobs/7")
    inbox.add("m-1", from_header="JOIN <noreply@join.com>",
              subject="Deine Bewerbung bei Anders Software GmbH",
              body="Vielen Dank, deine Bewerbung ist eingegangen. Unsere "
                   "Leuchte im Posteingang blinkt schon.", auth=VENDOR_AUTH)

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 0
    assert db.get_job(con, job_id)["bewerbung_id"] is None


async def test_a_refused_vendor_receipt_reaches_the_application_it_names(
        inbox, con):
    """Why refusing beats proposing. The same mail, with an application at
    the employer it actually names: the arm below finds it, and the posting
    the vendor domain happened to align with is left alone."""
    job_id = _strip_job(con, apply_url="https://join.com/companies/x/jobs/7")
    other = db.add_bewerbung(con, {"firma": "Anders Software GmbH",
                                   "kanal": "Online-Portal",
                                   "status": "Gesendet"})
    con.commit()
    inbox.add("m-1",
              from_header="Anders Software GmbH via JOIN <noreply@join.com>",
              subject="Deine Bewerbung bei Anders Software",
              body="Wir haben deine Bewerbung erhalten.", auth=VENDOR_AUTH)

    await service.ingest_replies()

    row = _inbound_rows(con)[0]
    assert (row["bewerbung_id"], row["matched_by"]) == (other, "name")
    assert db.get_job(con, job_id)["bewerbung_id"] is None


# the shelf files its own receipts
# --------------------------------------------------------------------------
def _shelf_receipt(con, *, bewerbung_id=None, job_id=None, message_id="shelf-1",
                   subject="Ihre Bewerbung ist eingegangen",
                   body="Vielen Dank, Ihre Bewerbung ist eingegangen.",
                   from_addr="hr@firma-beispiel.de",
                   internal_date="2026-09-02T10:00:00",
                   classified_by="rules") -> int:
    """A receipt already parked on the review shelf, as a pass would leave it."""
    row_id = db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": message_id,
        "from_addr": from_addr, "subject": subject, "body_text": body,
        "internal_date": internal_date, "bewerbung_id": bewerbung_id,
        "job_id": job_id, "matched_by": ("name" if bewerbung_id
                                         else service.MATCHED_RECEIPT),
        "classification": "eingang", "classified_by": classified_by,
        "needs_review": 1, "matched_note": "Firmenname"})
    con.commit()
    return row_id


async def test_a_receipt_for_an_application_that_now_exists_files_itself(
        inbox, con):
    """His shelf held 57 Eingangsbestätigungen and every one of them was for
    an application that already existed — 27 of them tied to nothing while the
    POSTING carried the application. Nothing ever looked again, so they piled
    up unanswered for weeks."""
    job_id = _strip_job(con)
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "kanal": "Online-Portal",
        "status": "Gesendet", "gesendet_am": "2026-09-01"})
    con.execute("UPDATE jobs SET bewerbung_id=? WHERE id=?",
                (bewerbung_id, job_id))
    row_id = _shelf_receipt(con, job_id=job_id,
                            subject="Ihre Bewerbung bei Firma Beispiel GmbH")

    outcome = await service.ingest_replies()

    assert outcome["attached"] == 1
    row = db.get_email_log(con, row_id)
    assert (row["needs_review"], row["bewerbung_id"], row["matched_by"]) \
        == (0, bewerbung_id, service.MATCHED_FILED)
    # the register is NOT touched: this pass has no sender verdict to write
    # from, so the status stays his and the arm with the headers keeps it
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"
    assert db.list_status_history(con, bewerbung_id) == [] or [
        str(h["source"]) for h in db.list_status_history(con, bewerbung_id)
    ] == ["user"]
    # and it says so in Gmail, without the waiting label
    assert inbox.label_calls[-1][:2] == ("shelf-1", ("L_JobDeck/Offen",))


async def test_a_receipt_already_tied_to_its_application_only_loses_the_shelf(
        inbox, con):
    """The other half of his shelf: the reply cascade had already tied the
    mail to the application and only the status was pending. The attachment is
    NOT restated — overwriting `matched_by` would take a name guess out of
    reach of a rescan, which is the one thing that can still correct it."""
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "kanal": "E-Mail",
        "status": "Gesendet", "gesendet_am": "2026-09-01"})
    row_id = _shelf_receipt(con, bewerbung_id=bewerbung_id)

    outcome = await service.ingest_replies()

    assert outcome["attached"] == 0          # nothing new was attached
    row = db.get_email_log(con, row_id)
    assert (row["needs_review"], row["matched_by"]) == (0, "name")
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"
    # AND the guess is still re-judgeable. What keeps it so is "no status
    # cites this row", not `matched_by` — a status write would have cemented
    # the very guesses the name arm was rewritten to correct.
    assert db.count_name_proposals(con, "2026-01-01T00:00:00") == 1


async def test_a_receipt_for_a_settled_application_is_filed_without_a_status(
        inbox, con):
    """Eleven of his hang off applications already answered. There is no
    decision left in them, so they leave the shelf — and the anti-downgrade
    rank is what keeps the register alone, not a second rule beside it."""
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "kanal": "E-Mail",
        "status": "Absage", "gesendet_am": "2026-09-01"})
    row_id = _shelf_receipt(con, bewerbung_id=bewerbung_id)

    await service.ingest_replies()

    assert db.get_email_log(con, row_id)["needs_review"] == 0
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Absage"
    # the register's only history is the row `add_bewerbung` wrote itself
    assert [str(h["source"]) for h in db.list_status_history(con, bewerbung_id)] \
        == ["user"]


async def test_a_receipt_older_than_its_application_stays_on_the_shelf(
        inbox, con):
    """A confirmation cannot precede the application it confirms. Five JOIN
    mails on his shelf are exactly this shape — and two more asked him to
    FINISH an application, which is not a receipt of one either."""
    job_id = _strip_job(con)
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "kanal": "Online-Portal",
        "status": "Gesendet", "gesendet_am": "2026-09-01"})
    con.execute("UPDATE jobs SET bewerbung_id=? WHERE id=?",
                (bewerbung_id, job_id))
    row_id = _shelf_receipt(con, job_id=job_id,
                            subject="Ihre Bewerbung bei Firma Beispiel GmbH",
                            internal_date="2026-08-12T10:00:00")

    outcome = await service.ingest_replies()

    assert outcome["attached"] == 0
    row = db.get_email_log(con, row_id)
    assert (row["needs_review"], row["bewerbung_id"]) == (1, None)
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"


async def test_an_application_without_a_send_date_is_never_attached_to(
        inbox, con):
    """The one direction of harm in this pass that was not conservative, found
    by the security review on its second pass.

    The register's form accepts an application with no date, and
    `identity.holds_company` then holds that company FOR EVER — "no usable date
    means the window cannot be proven to have passed". Attaching a mail to it
    gives `LAST_CONTACT_SQL` a usable date, so the cooling-off hold released and
    `services/send` stopped refusing a second application to a company he had
    already written to."""
    job_id = _strip_job(con)
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "kanal": "Online-Portal",
        "status": "Gesendet", "gesendet_am": ""})
    con.execute("UPDATE jobs SET bewerbung_id=? WHERE id=?",
                (bewerbung_id, job_id))
    row_id = _shelf_receipt(con, job_id=job_id,
                            from_addr="hr@firma-beispiel.de",
                            subject="Ihre Bewerbung bei Firma Beispiel GmbH",
                            internal_date="2019-01-02T09:00:00")

    outcome = await service.ingest_replies()

    assert outcome["attached"] == 0
    row = db.get_email_log(con, row_id)
    assert (row["needs_review"], row["bewerbung_id"]) == (1, None)
    # and the anchor the cooling-off gate reads has not moved
    held = [b for b in db.list_bewerbungen(con) if b["id"] == bewerbung_id][0]
    assert str(held["last_contact"] or "") == ""


async def test_a_new_attachment_has_to_name_the_employer(inbox, con):
    """The guard on the half that makes a NEW claim. A mail sitting on a
    posting because a vendor domain aligned with it names nobody, and the pass
    must not turn that into an attachment."""
    job_id = _strip_job(con, apply_url="https://join.com/companies/x/jobs/7")
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "kanal": "Online-Portal",
        "status": "Gesendet", "gesendet_am": "2026-09-01"})
    con.execute("UPDATE jobs SET bewerbung_id=? WHERE id=?",
                (bewerbung_id, job_id))
    row_id = _shelf_receipt(con, job_id=job_id,
                            from_addr="noreply@join.com",
                            subject="Deine Bewerbung bei Anders Software",
                            body="Wir haben deine Bewerbung erhalten.")

    outcome = await service.ingest_replies()

    assert outcome["attached"] == 0
    assert db.get_email_log(con, row_id)["needs_review"] == 1
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"


async def test_a_forged_display_name_cannot_move_the_register(inbox, con):
    """The security review's reproduction, kept as a test.

    `matchable_domain` refuses freemail, so a gmail.com sender gets no tenant
    tokens and no label keys — but `read_sender` computes `display_key` from
    the From header whatever the domain, so the company-name arm binds on the
    display name alone. That arm is documented as "a similarity, not an
    identification" and is not in the tier that may write; nothing on it ever
    asked about DMARC, because it never used to write. When the pass wrote
    statuses from the shelf, this mail moved his register."""
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "kanal": "Online-Portal",
        "status": "Gesendet", "gesendet_am": "2026-08-01"})
    con.commit()
    inbox.add("m-1", from_header="Firma Beispiel GmbH <angreifer@gmail.com>",
              subject="Ihre Bewerbung",
              body="vielen Dank, Ihre Bewerbung ist bei uns eingegangen.",
              auth=("mx.google.com; spf=pass smtp.mailfrom=gmail.com; "
                    "dmarc=pass header.from=gmail.com"))

    await service.ingest_replies()

    # the arm really did bind it — otherwise this test would pass for the
    # wrong reason, on a mail that matched nothing at all
    row = _inbound_rows(con)[0]
    assert (row["matched_by"], row["bewerbung_id"]) == ("name", bewerbung_id)
    assert row["classification"] == "eingang"
    # and the register did not move
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"
    assert [str(h["source"])
            for h in db.list_status_history(con, bewerbung_id)] == ["user"]


async def test_a_row_he_has_answered_is_never_reopened_by_the_pass(inbox, con):
    """`reply_manual` is his verdict and a status that cites the row is a
    decision already made. Both are left exactly as they stand."""
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "kanal": "E-Mail",
        "status": "Gesendet", "gesendet_am": "2026-09-01"})
    answered = _shelf_receipt(con, bewerbung_id=bewerbung_id,
                              classified_by="reply_manual")
    cited = _shelf_receipt(con, bewerbung_id=bewerbung_id,
                           message_id="shelf-2", subject="Zweite")
    db.add_status_history(con, bewerbung_id, "Gesendet", "In Bearbeitung",
                          "reply_auto", cited, "")
    con.commit()

    await service.ingest_replies()

    assert db.get_email_log(con, answered)["needs_review"] == 1
    assert db.get_email_log(con, cited)["needs_review"] == 1


async def test_a_receipt_the_pass_filed_never_anchors_its_gmail_thread(
        inbox, con):
    """The security review's third-pass CRITICAL, kept as a test.

    A thread match writes a status with no sender authentication at all — "a
    thread id is not forgeable" holds only while nothing but a writing tier can
    put one on an application. The pass files receipts on evidence whose own tier
    may only propose, so if its rows anchored a thread, the NEXT mail of that
    thread would close the application automatically. The review did exactly
    that with an outsider whose DMARC failed and whose only claim was the public
    Referenznummer, and the app's own evidence line on the attached row read
    „Absender gehört nicht zur Anzeige"."""
    # a sender the attach gate DOES accept, so this test isolates the anchoring
    # question from the question of who may attach at all
    job_id = _strip_job(con, apply_url="https://join.com/companies/x/jobs/7")
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "kanal": "Online-Portal",
        "status": "Gesendet", "gesendet_am": "2026-09-01"})
    con.execute("UPDATE jobs SET bewerbung_id=? WHERE id=?",
                (bewerbung_id, job_id))
    row_id = _shelf_receipt(con, job_id=job_id, message_id="shelf-thread",
                            from_addr="no-reply@msg.join.com",
                            subject="Deine Bewerbung bei der Firma Beispiel GmbH",
                            body="Wir haben deine Bewerbung erhalten.")
    con.execute("UPDATE email_log SET gmail_thread_id=? WHERE id=?",
                ("t-outsider", row_id))
    con.commit()

    await service.ingest_replies()
    assert db.get_email_log(con, row_id)["needs_review"] == 0   # it was filed

    # the mail's thread must NOT have become an anchor
    assert db.find_bewerbung_by_thread(con, "t-outsider") is None

    # and a second mail in that thread writes nothing — note it needs no
    # authentication at all: that is what the thread arm is allowed to skip
    inbox.add("m-2", from_header="Fremder <fremder@voellig-anders.example>",
              subject="Re: Ihre Bewerbung", body=ABSAGE_BODY,
              thread="t-outsider",
              auth=("mx.google.com; spf=pass smtp.mailfrom=voellig-anders."
                    "example; dmarc=fail header.from=voellig-anders.example"))
    await service.ingest_replies()

    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Gesendet"
    assert [str(h["source"])
            for h in db.list_status_history(con, bewerbung_id)] == ["user"]


async def test_only_a_writing_tier_or_his_own_verdict_anchors_a_thread(con):
    """The allowlist itself, value by value. It was a list of the arms that must
    NOT anchor, and the pass's new `matched_by` walked straight through it — so
    the rule is now stated the other way round and an unknown value fails
    closed."""
    bewerbung_id = db.add_bewerbung(con, {"firma": "Firma Beispiel GmbH",
                                          "status": "Gesendet"})
    may = ["thread", "address", "receipt", service.MATCHED_ATTACHED]
    may_not = ["name", "domain", service.MATCHED_FILED, service.MATCHED_UNDONE,
               "a-value-nobody-has-written-yet"]
    for i, matched_by in enumerate(may + may_not):
        thread = f"t-{i}"
        db.add_email_log(con, {
            "direction": "inbound", "gmail_message_id": f"anchor-{i}",
            "gmail_thread_id": thread, "bewerbung_id": bewerbung_id,
            "matched_by": matched_by, "classification": "eingang",
            "classified_by": "rules", "needs_review": 0})
        con.commit()
        found = db.find_bewerbung_by_thread(con, thread)
        if matched_by in may:
            assert found == bewerbung_id, matched_by
        else:
            assert found is None, matched_by
    # his own verdict anchors whatever the arm was
    db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": "anchor-his",
        "gmail_thread_id": "t-his", "bewerbung_id": bewerbung_id,
        "matched_by": "name", "classification": "eingang",
        "classified_by": "reply_manual", "needs_review": 0})
    con.commit()
    assert db.find_bewerbung_by_thread(con, "t-his") == bewerbung_id


async def test_prose_alone_cannot_attach_a_strangers_mail(inbox, con):
    """The vector behind that critical: the mail's own WORDS are written by
    whoever sent it, so on their own they let any mailbox attach itself to an
    application by naming the company. The words now count only from a domain
    receipts legitimately arrive through — a board or an ATS vendor. Measured on
    the corpus: dropping the prose arm entirely would have cost 10 of 18 genuine
    receipts, this costs one."""
    job_id = _strip_job(con)
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "kanal": "Online-Portal",
        "status": "Gesendet", "gesendet_am": "2026-09-01"})
    con.execute("UPDATE jobs SET bewerbung_id=? WHERE id=?",
                (bewerbung_id, job_id))
    stranger = _shelf_receipt(
        con, job_id=job_id, message_id="shelf-stranger",
        from_addr="angreifer@gmail.com",
        subject="Ihre Bewerbung bei der Firma Beispiel GmbH",
        body="Vielen Dank, Ihre Bewerbung ist eingegangen.")

    outcome = await service.ingest_replies()

    assert outcome["attached"] == 0
    row = db.get_email_log(con, stranger)
    assert (row["needs_review"], row["bewerbung_id"]) == (1, None)


async def test_prose_from_a_vendor_domain_still_attaches(inbox, con):
    """The other side of that condition, and why it is not a blanket refusal:
    JOIN and softgarden put nothing in their tenant slot, so on the real corpus
    ten of the eighteen attachments are justified by the mail's words alone."""
    job_id = _strip_job(con, apply_url="https://join.com/companies/x/jobs/7")
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "kanal": "Online-Portal",
        "status": "Gesendet", "gesendet_am": "2026-09-01"})
    con.execute("UPDATE jobs SET bewerbung_id=? WHERE id=?",
                (bewerbung_id, job_id))
    row_id = _shelf_receipt(
        con, job_id=job_id, message_id="shelf-vendor",
        from_addr="no-reply@msg.join.com",
        subject="Deine Bewerbung bei der Firma Beispiel GmbH",
        body="Wir haben deine Bewerbung erhalten.")

    outcome = await service.ingest_replies()

    assert outcome["attached"] == 1
    row = db.get_email_log(con, row_id)
    assert (row["needs_review"], row["matched_by"]) == (0, service.MATCHED_FILED)


async def test_adopting_a_receipt_he_took_back_can_be_undone_again(inbox, con):
    """`adopt_receipt` records a ledger row, so the row must say THIS app created
    it or the undo it earns is not offered. It did not need saying while
    `receipt` was the only value that could arrive there — a receipt he had taken
    back kept `receipt_undone`, so „Rückgängig" vanished and the row's own line
    told him it was taken back. Found by the security review."""
    _strip_job(con, apply_url="https://bewerbung.firma-beispiel.de/7")
    inbox.add("m-1", from_header="Firma <karriere@firma-beispiel.de>",
              subject="Ihre Bewerbung ist eingegangen",
              body="Vielen Dank, Ihre Bewerbung ist eingegangen.")
    await service.ingest_replies()
    row_id = int(_inbound_rows(con)[0]["id"])
    assert service.undo_receipt(row_id) is True
    assert db.get_email_log(con, row_id)["matched_by"] == service.MATCHED_UNDONE

    assert service.adopt_receipt(row_id)["ok"] is True

    row = db.get_email_log(con, row_id)
    assert row["matched_by"] == service.MATCHED_RECEIPT
    assert service.undo_receipt(row_id) is True      # and it really undoes


async def test_a_receipt_contradicting_a_silence_closure_stays_on_the_shelf(
        inbox, con):
    """„Keine Antwort" says nothing came back. This mail is something that came
    back, so the closure may well be wrong — the rank refuses to move it, and
    filing the mail anyway would take the evidence against it off the shelf
    while the register kept the closure. Found by the security review."""
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "kanal": "E-Mail",
        "status": "Keine Antwort", "gesendet_am": "2026-06-01"})
    row_id = _shelf_receipt(con, bewerbung_id=bewerbung_id)

    await service.ingest_replies()

    assert db.get_email_log(con, row_id)["needs_review"] == 1
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Keine Antwort"


async def test_a_receipt_he_took_back_is_never_filed_again(inbox, con):
    """His strongest no. `undo_receipt` restores the row as a plain receipt
    proposal, which is indistinguishable from one never judged — so the pass
    filed it again the moment the application existed, which after an undo is
    exactly when he records it himself. Found by the security review."""
    job_id = _strip_job(con)
    inbox.add("m-1", from_header="Firma <karriere@firma-beispiel.de>",
              subject="Ihre Bewerbung ist eingegangen",
              body="Vielen Dank, Ihre Bewerbung ist eingegangen.")
    con.execute("UPDATE jobs SET apply_url=? WHERE id=?",
                ("https://bewerbung.firma-beispiel.de/7", job_id))
    con.commit()
    await service.ingest_replies()          # records the application
    row = _inbound_rows(con)[0]
    assert service.undo_receipt(int(row["id"])) is True
    assert db.get_email_log(con, int(row["id"]))["needs_review"] == 1

    # he records it himself afterwards, which is the whole point of the undo
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "kanal": "Online-Portal",
        "status": "Gesendet", "gesendet_am": "2026-09-01"})
    con.execute("UPDATE jobs SET bewerbung_id=? WHERE id=?",
                (bewerbung_id, job_id))
    con.commit()

    outcome = await service.ingest_replies()

    assert outcome["attached"] == 0
    again = db.get_email_log(con, int(row["id"]))
    assert (again["needs_review"], again["bewerbung_id"]) == (1, None)


async def test_a_dismissal_he_pressed_while_the_shelf_was_walked_stands(
        inbox, con, monkeypatch):
    """The shelf is listed on one connection and acted on row by row on
    another, so a press of his can land in between. Without re-reading the row
    inside the write, the pass would attach a mail he had just pushed away —
    deciding from a snapshot that his press had already overtaken."""
    job_id = _strip_job(con)
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "kanal": "Online-Portal",
        "status": "Gesendet", "gesendet_am": "2026-09-01"})
    con.execute("UPDATE jobs SET bewerbung_id=? WHERE id=?",
                (bewerbung_id, job_id))
    row_id = _shelf_receipt(con, job_id=job_id,
                            subject="Ihre Bewerbung bei Firma Beispiel GmbH")
    stale = db.shelf_receipts(con)
    assert len(stale) == 1                      # the snapshot the pass reads
    monkeypatch.setattr(db, "shelf_receipts", lambda _con: stale)

    service.dismiss_review(row_id)               # his press, inside the window

    outcome = await service.ingest_replies()

    assert outcome["attached"] == 0
    row = db.get_email_log(con, row_id)
    assert (row["bewerbung_id"], row["classification"]) == (None, "")


async def test_the_shelf_is_filed_after_the_messages_of_the_same_pass(
        inbox, con):
    """A receipt this pass proposes is filed by this pass when the application
    is already there — the order is what makes the shelf never hold a row for
    a decision the same run could make."""
    job_id = _strip_job(con, apply_url="https://join.com/companies/x/jobs/7")
    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Firma Beispiel GmbH", "kanal": "Online-Portal",
        "status": "Gesendet", "gesendet_am": "2026-09-01"})
    con.execute("UPDATE jobs SET bewerbung_id=? WHERE id=?",
                (bewerbung_id, job_id))
    con.commit()
    # a spoofed sender: the receipt arm identifies the posting and refuses to
    # authorize, so the message becomes a proposal DURING this pass
    inbox.add("m-1", from_header="Firma Beispiel GmbH <hr@firma-beispiel.de>",
              subject="Ihre Bewerbung bei Firma Beispiel GmbH ist eingegangen",
              body="Vielen Dank für Ihre Bewerbung.", auth=AUTH_FAIL)

    outcome = await service.ingest_replies()

    assert outcome["attached"] == 1
    row = _inbound_rows(con)[0]
    assert (row["needs_review"], row["bewerbung_id"]) == (0, bewerbung_id)


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
        con, apply_url="https://de.jooble.org/away/1234567890123456789",
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
        con, apply_url="https://de.jooble.org/away/1234567890123456789",
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


async def test_a_vendor_domain_reaches_the_tenant_not_a_lookalike(inbox, con):
    """Measured on his mailbox 2026-09-11: five Personio-sent mails from four
    different employers, two of them rejections, were proposed for one
    application whose name shares its first six letters — a six-character prefix of
    `personio` matched it. The employer of a vendor's mail is in the tenant
    slot, and the look-alike must not be touched."""
    lookalike = _form_application(con, firma="Personalfrage Beispiel GmbH")
    tenant = _form_application(con, firma="Beispiel GmbH")
    inbox.add("m-1", from_header="Recruiting Team <beispiel-jobs@m.personio.de>",
              body=ABSAGE_BODY)

    await service.ingest_replies()

    row = _inbound_rows(con)[0]
    assert (row["bewerbung_id"], row["matched_by"]) == (tenant, "name")
    assert row["bewerbung_id"] != lookalike
    assert row["needs_review"] == 1


async def test_the_next_mail_of_a_proposed_thread_is_a_proposal_too(inbox, con):
    """A name proposal must not become an automatic write one mail later:
    the follow-up in the same thread matched by `thread` — the tier that
    writes, without DMARC — and would have filed a rejection on whatever
    application the resemblance had picked."""
    bewerbung_id = _form_application(con)
    inbox.add("m-1", from_header="Firma Beispiel GmbH <hr@irgendwo-anders.de>",
              subject="Ihre Bewerbung", body="Vielen Dank, wir melden uns.",
              thread="t-shared")
    await service.ingest_replies()
    first = _inbound_rows(con)[0]
    assert (first["matched_by"], first["needs_review"]) == ("name", 1)

    inbox.add("m-2", from_header="Firma Beispiel GmbH <hr@irgendwo-anders.de>",
              subject="AW: Ihre Bewerbung", body=ABSAGE_BODY, thread="t-shared")
    outcome = await service.ingest_replies()

    second = _inbound_rows(con)[-1]
    assert second["matched_by"] != "thread"
    assert second["needs_review"] == 1
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


async def test_a_vendor_domain_never_domain_matches(inbox, con):
    """A vendor address stored as an application's contact — a JOIN inbox,
    a Personio no-reply — would make every mail from that vendor look like
    that application's. The domain names the vendor, not the employer."""
    _sent_application(con, email_addr="jobs@join.com")
    inbox.add("m-1", from_header="Andere Firma <no-reply@msg.join.com>",
              body=ABSAGE_BODY)

    await service.ingest_replies()

    assert _inbound_rows(con) == []


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


def _name_proposal(con, bewerbung_id: int, message_id: str, *,
                   days_ago: float = 1, classified_by: str = "rules",
                   needs_review: int = 1) -> int:
    """A row the company-name arm produced: proposed, never written."""
    stamp = (datetime.datetime.now() - datetime.timedelta(days=days_ago)
             ).isoformat(timespec="seconds")
    row_id = db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": message_id,
        "from_addr": "beispiel-jobs@m.personio.de", "subject": "Absage",
        "internal_date": stamp, "bewerbung_id": bewerbung_id,
        "matched_by": "name", "classification": "absage",
        "classified_by": classified_by, "needs_review": needs_review})
    con.commit()
    return row_id


async def test_a_rescan_rejudges_the_name_proposals_he_has_not_answered(
        inbox, con):
    """The first name rule had put sixteen of his real mails on the wrong
    application, and nothing could ever move them: a matched row is never
    re-read. A proposal he has not answered carries nothing of his, so a
    better rule is allowed to re-place it — here from the look-alike the old
    prefix chose to the tenant the vendor's address actually names."""
    lookalike = _form_application(con, firma="Personalfrage Beispiel GmbH")
    tenant = _form_application(con, firma="Beispiel GmbH")
    old_row = _name_proposal(con, lookalike, "m-1")
    inbox.add("m-1", from_header="Recruiting Team <beispiel-jobs@m.personio.de>",
              body=ABSAGE_BODY)

    result = service.rescan()

    assert result["rejudged"] == 1
    # marked, not dropped: the pass that lists the message drops the row
    # right before reading it again — the rescan itself touches neither the
    # row nor Gmail
    assert [row["id"] for row in _inbound_rows(con)] == [old_row]
    assert db.get_setting(con, service.REJUDGE_KEY, "") != ""
    assert inbox.label_calls == []

    await service.ingest_replies()

    rows = _inbound_rows(con)
    assert len(rows) == 1 and rows[0]["id"] != old_row
    assert (rows[0]["bewerbung_id"], rows[0]["matched_by"]) == (tenant, "name")
    assert rows[0]["bewerbung_id"] != lookalike
    # its old labels came down before the re-read — all of them, so a message
    # the re-read then ignores cannot keep saying it is waiting for him
    assert ("m-1", (), tuple(sorted(f"L_{n}" for n in service.ALL_LABELS))) \
        in inbox.label_calls
    assert db.get_setting(con, service.REJUDGE_KEY, "") == ""


async def test_a_rescan_keeps_the_name_rows_he_judged_or_that_wrote_a_status(
        inbox, con):
    """His hand's work is exactly what a re-judge must never undo."""
    bewerbung_id = _form_application(con)
    judged = _name_proposal(con, bewerbung_id, "m-judged",
                            classified_by="reply_manual", needs_review=0)
    written = _name_proposal(con, bewerbung_id, "m-written")
    service.resolve_review(written, "absage", force_status=True)
    assert any(h["email_log_id"] == written
               for h in db.list_status_history(con, bewerbung_id))
    # a status that cites the row, however it was written — no automatic
    # writer takes the name arm today, and the guard must not depend on that
    other = _form_application(con, firma="Zweite Beispiel GmbH")
    cited = _name_proposal(con, other, "m-cited")
    db.set_status(con, other, "Absage", source="reply_auto",
                  email_log_id=cited)
    con.commit()
    # his dismissal is a verdict too: "x" unlinks the row and settles it,
    # but leaves `matched_by` saying name — and "Alle ablegen" does that to
    # a whole view at once
    dismissed = _name_proposal(con, bewerbung_id, "m-dismissed")
    service.dismiss_review(dismissed)
    reopened = _name_proposal(con, bewerbung_id, "m-reopened")
    service.dismiss_review(reopened)
    service.reopen_review(reopened)
    untouched = _name_proposal(con, bewerbung_id, "m-untouched")
    # every one of them is listed again by the next sync
    for message_id in ("m-judged", "m-written", "m-cited", "m-dismissed",
                       "m-reopened", "m-untouched"):
        inbox.add(message_id, body=ABSAGE_BODY)

    result = service.rescan()
    assert result["rejudged"] == 1

    await service.ingest_replies()

    ids = {row["id"] for row in _inbound_rows(con)}
    assert {judged, written, cited, dismissed, reopened} <= ids
    assert untouched not in ids


async def test_a_cut_off_listing_drops_only_what_it_lists(
        inbox, con, monkeypatch):
    """A full sync lists the newest messages of the window up to its bound.
    A row dropped for a message it does not list would be gone for good —
    body, classification and link — with no message. So the rescan only
    marks, and the pass drops a row right before it reads the message."""
    bewerbung_id = _form_application(con)
    listed = _name_proposal(con, bewerbung_id, "m-listed")
    unlisted = _name_proposal(con, bewerbung_id, "m-unlisted")
    inbox.add("m-listed", body=ABSAGE_BODY)
    monkeypatch.setattr(service, "LIST_AHEAD", 1)   # the bound cut it off

    result = service.rescan()
    assert result["rejudged"] == 2               # what qualifies: a bound

    await service.ingest_replies()

    ids = {row["id"] for row in _inbound_rows(con)}
    assert unlisted in ids and listed not in ids
    assert db.get_email_log(con, unlisted)["bewerbung_id"] == bewerbung_id
    assert db.get_setting(con, service.REJUDGE_KEY, "") == ""


async def test_a_complete_listing_drops_the_proposals_for_mail_that_is_gone(
        inbox, con):
    """A listing the bound did not cut off holds every message of the
    window. A qualifying row it does not hold is for mail that has left the
    mailbox — on his data 18 of 59 were newsletters he had since deleted —
    and nothing can ever read it again; it goes too, and no label call is
    made for a message that is not there."""
    bewerbung_id = _form_application(con)
    listed = _name_proposal(con, bewerbung_id, "m-listed")
    gone = _name_proposal(con, bewerbung_id, "m-gone")
    inbox.add("m-listed", body=ABSAGE_BODY)      # one message, bound 500

    service.rescan()
    await service.ingest_replies()

    ids = {row["id"] for row in _inbound_rows(con)}
    assert gone not in ids and listed not in ids
    labelled = {call[0] for call in inbox.label_calls}
    assert "m-listed" in labelled and "m-gone" not in labelled


async def test_a_rescan_keeps_a_name_row_outside_the_window(inbox, con):
    """The window is the one the sync lists; a row dated before it is not a
    proposal the sync can re-judge."""
    bewerbung_id = _form_application(con)
    inside = _name_proposal(con, bewerbung_id, "m-inside", days_ago=10)
    outside = _name_proposal(con, bewerbung_id, "m-outside", days_ago=100)
    inbox.add("m-inside", body=ABSAGE_BODY)
    inbox.add("m-outside", body=ABSAGE_BODY)

    result = service.rescan(lookback_days=30)
    assert result["rejudged"] == 1

    await service.ingest_replies()

    ids = {row["id"] for row in _inbound_rows(con)}
    assert outside in ids and inside not in ids


async def test_the_rejudge_window_is_the_one_he_just_chose(inbox, con):
    """The dialog lets him widen the window in the same press; the re-judge
    has to read the widened value, not the one stored before it."""
    bewerbung_id = _form_application(con)
    _name_proposal(con, bewerbung_id, "m-inside", days_ago=10)
    _name_proposal(con, bewerbung_id, "m-outside", days_ago=100)

    result = service.rescan(lookback_days=200)

    assert result["rejudged"] == 2


async def test_the_drain_after_a_rejudge_stays_a_full_sync(
        inbox, con, monkeypatch):
    """A pass is bounded; a re-judge with more messages than one pass reads
    drains over several. When a pass in flight during the rescan had stored
    its checkpoint, the passes after the first would have gone back to the
    incremental read and never listed the rest."""
    bewerbung_id = _form_application(con)
    _name_proposal(con, bewerbung_id, "m-1")
    _name_proposal(con, bewerbung_id, "m-2")
    inbox.add("m-1", body=ABSAGE_BODY)
    inbox.add("m-2", body=ABSAGE_BODY)
    monkeypatch.setattr(service, "MAX_MESSAGES_PER_PASS", 1)
    service.rescan()
    with db.db() as write:
        db.set_setting(write, service.HISTORY_KEY, "h-restored")
    monkeypatch.setattr(
        gmail, "history_added_messages",
        lambda *a: pytest.fail("incremental read while draining a re-judge"))

    first = await service.ingest_replies()
    second = await service.ingest_replies()

    assert (first["seen"], second["seen"]) == (1, 1)
    assert {row["gmail_message_id"] for row in _inbound_rows(con)} \
        == {"m-1", "m-2"}


async def test_a_pending_rejudge_forces_a_full_sync(inbox, con, monkeypatch):
    """A rescan racing a pass in flight can see that pass store its
    checkpoint after the rescan cleared it. The mark outlives that and still
    makes the next pass a full sync — otherwise the re-judge would wait for
    the next rescan."""
    bewerbung_id = _form_application(con)
    _name_proposal(con, bewerbung_id, "m-1")
    inbox.add("m-1", body=ABSAGE_BODY)
    service.rescan()
    with db.db() as write:
        db.set_setting(write, service.HISTORY_KEY, "h-restored")
    monkeypatch.setattr(
        gmail, "history_added_messages",
        lambda *a: pytest.fail("incremental read despite a pending re-judge"))

    await service.ingest_replies()

    assert db.get_setting(con, service.REJUDGE_KEY, "") == ""


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


def test_adopting_a_receipt_cannot_reopen_a_closed_application(inbox, con):
    """Found by the review panel: "Als Bewerbung eintragen" wrote
    'In Bearbeitung' unconditionally with source='reply_manual', which the
    anti-downgrade rank exempts — so the one button the verdict guard does not
    cover was a way around it, on the very screen that states the rule."""
    job_id = _strip_job(con)
    row_id = db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": "m-r", "job_id": job_id,
        "matched_by": service.MATCHED_RECEIPT, "classification": "eingang",
        "needs_review": 1})
    bewerbung_id = _sent_application(con)
    db.set_status(con, bewerbung_id, "Absage", source="user")
    db.set_job_status(con, job_id, "applied", bewerbung_id=bewerbung_id)
    con.commit()

    assert service.adopt_receipt(row_id)["ok"] is True

    # the mail is attached and settled ...
    row = db.get_email_log(con, row_id)
    assert (row["bewerbung_id"], row["needs_review"]) == (bewerbung_id, 0)
    # ... and the closed application stands
    assert db.get_bewerbung(con, bewerbung_id)["status"] == "Absage"


def test_adopting_a_receipt_still_raises_an_open_application(inbox, con):
    """The guard is about going backwards; the ordinary case must be
    untouched."""
    job_id = _strip_job(con)
    row_id = db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": "m-r", "job_id": job_id,
        "matched_by": service.MATCHED_RECEIPT, "classification": "eingang",
        "needs_review": 1})
    bewerbung_id = _sent_application(con)
    db.set_job_status(con, job_id, "applied", bewerbung_id=bewerbung_id)
    con.commit()

    assert service.adopt_receipt(row_id)["ok"] is True

    assert db.get_bewerbung(con, bewerbung_id)["status"] == "In Bearbeitung"


# --------------------------------------------------------------------------
# a board may never authorize, whatever the channel column says today
# --------------------------------------------------------------------------
async def test_a_board_mail_cannot_confirm_after_a_contact_address_moved_the_channel(
        inbox, con):
    """The hole the first board guard left open, and it fired for real.

    The guard asked the row's CHANNEL. Entering a contact address by hand —
    the ordinary way a posting found on a board becomes sendable — moves
    that column to direct_email, while `apply_url` still holds the board
    link the posting arrived by. The board's own domain then aligned with
    the posting and a routine notification from it wrote a status onto a
    live application."""
    job_id = _strip_job(
        con,
        apply_url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1",
        apply_channel="direct_email",
        contact_email="info@firma-beispiel.de")
    inbox.add("m-1", from_header="Terminservice <termin@arbeitsagentur.de>",
              subject="Stornierung Ihres Termins",
              body="Ihr Termin wurde storniert. Ihre Bewerbung ist "
                   "eingegangen.",
              auth=("mx.google.com; spf=pass smtp.mailfrom=arbeitsagentur.de; "
                    "dmarc=pass header.from=arbeitsagentur.de"))

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 0
    assert db.get_job(con, job_id)["bewerbung_id"] is None
    assert _inbound_rows(con) == []


async def test_a_board_mail_cannot_confirm_on_an_ats_channel_posting(inbox, con):
    """The same shape as above and the commonest one: the channel resolves to
    an ATS form while `apply_url` is still the board's link."""
    job_id = _strip_job(
        con, apply_url="https://www.arbeitnow.com/jobs/companies/x/y",
        apply_channel="ats_form")
    inbox.add("m-1", from_header="Arbeitnow <alerts@arbeitnow.com>",
              subject="Eingangsbestätigung",
              body="Ihre Bewerbung ist eingegangen.",
              auth=("mx.google.com; spf=pass smtp.mailfrom=arbeitnow.com; "
                    "dmarc=pass header.from=arbeitnow.com"))

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 0
    assert db.get_job(con, job_id)["bewerbung_id"] is None


async def test_a_board_domain_in_the_contact_column_cannot_authorize(inbox, con):
    """The other column reaches the same target set. He types this field by
    hand, so nothing stops a board address from landing in it."""
    job_id = _strip_job(con, apply_url="",
                        apply_channel="direct_email",
                        contact_email="vermittlung@arbeitsagentur.de")
    inbox.add("m-1", from_header="Agentur <vermittlung@arbeitsagentur.de>",
              subject="Eingangsbestätigung",
              body="Ihre Bewerbung ist eingegangen.",
              auth=("mx.google.com; spf=pass smtp.mailfrom=arbeitsagentur.de; "
                    "dmarc=pass header.from=arbeitsagentur.de"))

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 0
    assert db.get_job(con, job_id)["bewerbung_id"] is None


async def test_a_board_mail_quoting_the_refnr_becomes_a_proposal(inbox, con):
    """The designed degradation, and the reason the guard is on AUTHORIZATION
    rather than on identification: the Refnr is printed in the public advert,
    so quoting it still says WHICH posting is meant. That is worth showing
    him — it just may not write."""
    job_id = _strip_job(
        con, refnr="10000-1177449Z",
        apply_url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1",
        apply_channel="direct_email",
        contact_email="info@firma-beispiel.de")
    inbox.add("m-1", from_header="Agentur <noreply@arbeitsagentur.de>",
              subject="Eingangsbestätigung Referenz 10000-1177449Z",
              body="Ihre Bewerbung ist eingegangen.",
              auth=("mx.google.com; spf=pass smtp.mailfrom=arbeitsagentur.de; "
                    "dmarc=pass header.from=arbeitsagentur.de"))

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 0
    assert outcome["review"] == 1
    assert db.get_job(con, job_id)["bewerbung_id"] is None
    row = _inbound_rows(con)[0]
    assert row["needs_review"] == 1
    assert row["job_id"] == job_id
    # it says WHY it is only a proposal, so he is not left guessing
    assert row["matched_note"] == ("Refnr 10000-1177449Z"
                                  " · Absender gehört nicht zur Anzeige")


async def test_the_employers_address_still_authorizes_on_a_board_found_posting(
        inbox, con):
    """The guard must cost nothing on the row it protects: the SAME posting,
    board link and all, still records from the employer's own domain."""
    job_id = _strip_job(
        con,
        apply_url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1",
        apply_channel="direct_email",
        contact_email="info@firma-beispiel.de")
    inbox.add("m-1", from_header="Firma <info@firma-beispiel.de>",
              subject="Eingangsbestätigung",
              body="Ihre Bewerbung ist eingegangen.",
              auth=("mx.google.com; spf=pass smtp.mailfrom=firma-beispiel.de; "
                    "dmarc=pass header.from=firma-beispiel.de"))

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 1
    job = db.get_job(con, job_id)
    assert job["bewerbung_id"] is not None
    assert db.get_bewerbung(con, job["bewerbung_id"])["status"] == "In Bearbeitung"


async def test_a_stale_channel_column_does_not_decide_who_may_authorize(
        inbox, con):
    """The rule reads the URL, not the column, and that cuts both ways.

    A row can be entered by hand with a channel that disagrees with its own
    apply_url. Judging by the column would refuse the employer's own
    e-recruiting host here — and it is exactly the column's mutability that
    let a board through in the first place, so the fact wins."""
    job_id = _strip_job(
        con, apply_url="https://firma.mein-beispiel-portal.de/stelle-1",
        apply_channel="board_apply")   # stale: the URL is no board
    inbox.add("m-1", from_header="Portal <no-reply@mein-beispiel-portal.de>",
              subject="Eingangsbestätigung",
              body="Ihre Bewerbung ist eingegangen.",
              auth=("mx.google.com; "
                    "spf=pass smtp.mailfrom=mein-beispiel-portal.de; "
                    "dmarc=pass header.from=mein-beispiel-portal.de"))

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 1
    assert db.get_job(con, job_id)["bewerbung_id"] is not None


# --------------------------------------------------------------------------
# a receipt cannot predate the form opening it confirms
# --------------------------------------------------------------------------
def _opened_ago(con, job_id, hours: float) -> str:
    """Stamp the form opening `hours` back and hand the stamp over.

    Relative to the clock on purpose: the candidate window is 72 hours
    wide, so a fixed date would fall out of it and the test would pass by
    never reaching the guard at all."""
    stamp = (datetime.datetime.now() - datetime.timedelta(hours=hours)
             ).isoformat(timespec="seconds")
    con.execute("UPDATE jobs SET form_opened_at=? WHERE id=?", (stamp, job_id))
    con.commit()
    return stamp


async def test_a_mail_older_than_the_form_opening_cannot_confirm_it(inbox, con):
    """The candidate window measures the POSTING's age, never the mail's, and
    every re-read of the mailbox walks months of old mail past this arm. A
    notification from before he opened the form is not its receipt."""
    job_id = _strip_job(con, apply_url="https://bewerbung.firma-beispiel.de/7")
    opened = _opened_ago(con, job_id, 2)
    inbox.add("m-1", from_header="Firma <karriere@firma-beispiel.de>",
              subject="Eingangsbestätigung",
              body="Ihre Bewerbung ist eingegangen.",
              internal_date_ms=_ms(opened) - 1000)

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 0
    assert outcome["review"] == 1
    assert db.get_job(con, job_id)["bewerbung_id"] is None
    row = _inbound_rows(con)[0]
    assert row["needs_review"] == 1
    assert row["matched_note"].endswith("· älter als die Bewerbung")


async def test_a_receipt_that_follows_the_opening_still_records(inbox, con):
    """The guard must not cost the feature: same posting, same sender, a
    mail dated AFTER he opened the form."""
    job_id = _strip_job(con, apply_url="https://bewerbung.firma-beispiel.de/7")
    opened = _opened_ago(con, job_id, 2)
    inbox.add("m-1", from_header="Firma <karriere@firma-beispiel.de>",
              subject="Eingangsbestätigung",
              body="Ihre Bewerbung ist eingegangen.",
              internal_date_ms=_ms(opened) + 1000)

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 1
    assert db.get_job(con, job_id)["bewerbung_id"] is not None


async def test_a_mail_stamped_at_the_very_moment_of_opening_still_records(
        inbox, con):
    """The boundary is inclusive on purpose: both stamps have one-second
    resolution, so an ATS answering inside the same second is a real
    receipt, not a mail from the past."""
    job_id = _strip_job(con, apply_url="https://bewerbung.firma-beispiel.de/7")
    opened = _opened_ago(con, job_id, 2)
    inbox.add("m-1", from_header="Firma <karriere@firma-beispiel.de>",
              subject="Eingangsbestätigung",
              body="Ihre Bewerbung ist eingegangen.",
              internal_date_ms=_ms(opened))

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 1


async def test_an_undated_mail_cannot_confirm_anything(inbox, con):
    """Gmail does hand back messages with no internalDate — his log holds
    such rows. Nothing can be shown to follow the opening, so it waits."""
    job_id = _strip_job(con, apply_url="https://bewerbung.firma-beispiel.de/7")
    inbox.add("m-1", from_header="Firma <karriere@firma-beispiel.de>",
              subject="Eingangsbestätigung",
              body="Ihre Bewerbung ist eingegangen.",
              internal_date_ms=0)

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 0
    assert outcome["review"] == 1
    assert db.get_job(con, job_id)["bewerbung_id"] is None


# --------------------------------------------------------------------------
# _follows_the_opening — every arm, including the one ingestion cannot reach
# --------------------------------------------------------------------------
def _meta_at(stamp: str | None) -> dict:
    return {"internal_date_ms": 0 if stamp is None else _ms(stamp)}


@pytest.mark.parametrize("opened, arrived, expected", [
    ("2026-08-19T15:00:00", "2026-08-19T15:00:01", True),   # after
    ("2026-08-19T15:00:00", "2026-08-19T15:00:00", True),   # same second
    ("2026-08-19T15:00:00", "2026-08-19T14:59:59", False),  # one second before
    ("2026-08-19T15:00:00", "2026-07-07T11:26:40", False),  # weeks before
    ("2026-08-19T15:00:00", "2027-01-01T00:00:00", True),   # long after
])
def test_a_receipt_must_not_predate_the_opening(opened, arrived, expected):
    job = {"form_opened_at": opened}
    assert service._follows_the_opening(job, _meta_at(arrived)) is expected


def test_a_mail_without_a_date_follows_nothing():
    """Gmail does return messages with no internalDate. Nothing can be shown,
    so nothing is authorized."""
    job = {"form_opened_at": "2026-08-19T15:00:00"}
    assert service._follows_the_opening(job, _meta_at(None)) is False


@pytest.mark.parametrize("opened", ["", FORM_OPENED_UNKNOWN])
def test_a_posting_with_no_real_opening_moment_authorizes_nothing(opened):
    """`unbekannt` is what a pre-v10 row carries instead of a moment. The
    candidate query filters those out today, so this arm is unreachable from
    ingestion — it is asserted here because the predicate is the thing that
    states the rule, and a later caller must not have to rediscover it."""
    job = {"form_opened_at": opened}
    assert service._follows_the_opening(job, _meta_at("2026-08-19T15:00:00")) \
        is False


def test_the_explicit_refusals_do_not_rest_on_how_a_stamp_sorts():
    """Two checks in `_follows_the_opening` are EQUIVALENT MUTATIONS today —
    verified, not assumed — and are kept deliberately.

    Deleting `bool(arrived)` or the `FORM_OPENED_UNKNOWN` comparison leaves
    the suite green, because an empty stamp sorts before every ISO date and
    ASCII digits sort before letters. Both are accidents of the two strings
    we happen to use, not properties of "no date". Respell the sentinel as
    `-` or `0000-unknown` — either an ordinary choice — and the accident
    flips to True, which authorizes a write on a posting that never
    recorded an opening.
    """
    iso = "2026-08-19T15:00:00"
    # today's accident, and how ordinarily it flips
    assert (iso >= FORM_OPENED_UNKNOWN) is False
    assert (iso >= "0000-unknown") is True
    assert ("" >= iso) is False
    # what the explicit checks state regardless of either
    assert service._follows_the_opening({"form_opened_at": ""},
                                        _meta_at(iso)) is False
    assert service._follows_the_opening({"form_opened_at": FORM_OPENED_UNKNOWN},
                                        _meta_at(iso)) is False
    assert service._follows_the_opening({"form_opened_at": iso},
                                        _meta_at(None)) is False


# --------------------------------------------------------------------------
# a receipt must be SAID — "we read nothing" is not "your application arrived"
# --------------------------------------------------------------------------
async def test_a_mail_the_rules_cannot_read_never_records_an_application(
        inbox, con):
    """The defect that fired on his real mailbox, in the shape it fired in.

    A job platform's own account mail arrives from the very domain the
    posting applies through, so it is strong enough to authorize — and it
    says nothing the German rules recognise. The classification defaulted to
    'eingang', so "we recognised none of this" was filed as "your
    application arrived": an application was recorded to an employer nothing
    had been sent to, and that company's one slot was spent."""
    job_id = _strip_job(
        con, apply_url="https://portal-beispiel.de/stellen/7",
        apply_channel="ats_form")
    inbox.add("m-1", from_header="Portal <mail@info.portal-beispiel.de>",
              subject="Bitte bestätige Deine E-Mail-Adresse",
              body="Willkommen! Bitte bestätige Deine E-Mail-Adresse mit "
                   "einem Klick auf den Link. Viel Glück bei Deiner "
                   "Bewerbung! Du erhältst diese E-Mail, weil Du Dich "
                   "angemeldet hast.",
              auth=("mx.google.com; "
                    "spf=pass smtp.mailfrom=portal-beispiel.de; "
                    "dmarc=pass header.from=portal-beispiel.de"))

    outcome = await service.ingest_replies()

    # premise: the rules really do read nothing here, so this test is about
    # what the arm does with silence rather than about a missing pattern
    assert replies.classify("Bitte bestätige Deine E-Mail-Adresse",
                            "Willkommen! Bitte bestätige Deine "
                            "E-Mail-Adresse.") is None
    assert outcome["receipts"] == 0
    assert outcome["review"] == 1
    assert db.get_job(con, job_id)["bewerbung_id"] is None
    assert db.get_job(con, job_id)["status"] == "new"
    row = _inbound_rows(con)[0]
    assert row["needs_review"] == 1
    # and it does not CLAIM to be a receipt on the screen either
    assert row["classification"] == ""


async def test_a_stated_receipt_from_the_same_sender_still_records(inbox, con):
    """The guard must not cost the feature: the same domain, the same
    posting, a mail that actually says the application arrived."""
    job_id = _strip_job(
        con, apply_url="https://portal-beispiel.de/stellen/7",
        apply_channel="ats_form")
    inbox.add("m-1", from_header="Portal <mail@info.portal-beispiel.de>",
              subject="Eingangsbestätigung",
              body="Ihre Bewerbung ist eingegangen.",
              auth=("mx.google.com; "
                    "spf=pass smtp.mailfrom=portal-beispiel.de; "
                    "dmarc=pass header.from=portal-beispiel.de"))

    outcome = await service.ingest_replies()

    assert outcome["receipts"] == 1
    job = db.get_job(con, job_id)
    assert job["bewerbung_id"] is not None
    assert db.get_bewerbung(con, job["bewerbung_id"])["status"] == "In Bearbeitung"
    assert _inbound_rows(con)[0]["classification"] == "eingang"


async def test_the_polite_opener_alone_records_no_application(inbox, con):
    """`replies.py` calls the courtesy opener the weakest evidence in the
    module and says it must never write a status — but this arm asked only
    WHAT family the rules read, never how sure they were, so the opener
    alone recorded an application.

    The rule-level tests carry that invariant in their NAMES and assert only
    `confident is False`; the write went unchecked. This asserts the write."""
    job_id = _strip_job(
        con, apply_url="https://portal-beispiel.de/stellen/7",
        apply_channel="ats_form")
    inbox.add("m-1", from_header="Portal <mail@info.portal-beispiel.de>",
              subject="Thank you for your application",
              body="Hello! Thanks for your application and your interest in "
                   "joining us, we will reach out if your profile matches.",
              auth=("mx.google.com; spf=pass smtp.mailfrom=portal-beispiel.de; "
                    "dmarc=pass header.from=portal-beispiel.de"))

    outcome = await service.ingest_replies()

    # premise: the rules DO read it, but only as courtesy
    verdict = replies.classify(
        "Thank you for your application",
        "Hello! Thanks for your application and your interest in joining us.")
    assert verdict is not None and verdict.confident is False

    assert outcome["receipts"] == 0
    assert outcome["review"] == 1
    assert db.get_job(con, job_id)["bewerbung_id"] is None
    assert db.get_job(con, job_id)["status"] == "new"
    assert _inbound_rows(con)[0]["needs_review"] == 1
