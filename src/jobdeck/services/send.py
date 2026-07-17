"""Human-approved sending of a drafted application via Gmail.

The path is fail-closed around one hard rule: while real sending is OFF
(the default), every message goes to the user's own configured test
recipient — and to nobody else. No test recipient configured means no
send at all. Test sends leave the audit trail ('outbound_test' in
email_log, counting toward the daily cap — the account's reputation does
not care who the recipient was) but never touch the draft's lifecycle,
the job, or the applications table, so the same draft can be test-sent
repeatedly and still be really sent later.

Unlike drafting, a claim stuck in 'sending' is NEVER reclaimed
automatically: a double-send to a company cannot be taken back, so only
a human (who can check the Gmail Sent folder) may resolve it, from the
review queue. The same reasoning governs ambiguous transport failures
(GmailUncertain): the message may already have been accepted, so the
claim stays put instead of inviting a retry.

Callers pin what they believe they are sending via `expect` — the
content and mode a human confirmed, or the status the auto-send worker
picked. Anything that changed since means the approval no longer
describes the message, and the send is refused rather than sending
something the user never agreed to.
"""

import asyncio
import datetime
import logging
import pathlib

from jobdeck import db, gmail
from jobdeck.constants import EMAIL_OUTBOUND, EMAIL_OUTBOUND_TEST
from jobdeck.dedupe import find_duplicate_bewerbung

log = logging.getLogger(__name__)

_lock = asyncio.Lock()  # one send at a time — manual clicks and auto-send alike

SNIPPET_CHARS = 120

# Content fields that define the message. A change to any of them makes an
# earlier approval (or a pre-send snapshot) describe a different e-mail.
CONTENT_FIELDS = ("updated_at", "recipient", "betreff", "email_body",
                  "anschreiben_body", "pdf_path")


def _error(message: str, kind: str = "draft") -> dict:
    """kind='global' marks a condition no draft is at fault for (cap, no
    connection) — the auto-send worker must pause rather than blame the
    draft it happened to pick."""
    return {"ok": False, "error": message, "kind": kind, "test_mode": False,
            "recipient": "", "draft": None}


def _load_context(job_id: int) -> tuple[dict | None, dict | None, dict]:
    with db.db() as con:
        draft = db.get_draft_by_job(con, job_id)
        job = db.get_job(con, job_id)
        settings = {
            "real_send_enabled": db.get_setting(con, "real_send_enabled", "0"),
            "test_recipient": db.get_setting(con, "test_recipient", "").strip(),
            "daily_send_cap": db.get_setting(con, "daily_send_cap", "15"),
            "applicant_name": db.get_setting(con, "applicant_name", "").strip(),
            "gmail_address": db.get_setting(con, "gmail_address", "").strip(),
        }
        sent_today = db.count_outbound_today(con)
    return (
        dict(draft) if draft is not None else None,
        dict(job) if job is not None else None,
        {**settings, "sent_today": sent_today},
    )


def resolve_recipient(draft_recipient: str, settings: dict) -> tuple[str, bool, str]:
    """The address a send would actually go to: (recipient, test_mode, error).

    Fail closed: real mode needs the user's explicit opt-in setting; test
    mode refuses to guess an address when none is configured."""
    if settings["real_send_enabled"] != "1":
        test = settings["test_recipient"]
        if not test:
            return "", True, (
                "real sending is OFF and no test recipient is set — add one "
                "in Settings (Sending) to send test e-mails"
            )
        return test, True, ""
    return (draft_recipient or "").strip(), False, ""


def expectation_mismatch(expect: dict, draft: dict, recipient: str,
                         test_mode: bool) -> str:
    """What the caller pinned vs what would actually be sent now."""
    if "status" in expect and draft["status"] != expect["status"]:
        return (f"this draft is no longer {expect['status']} "
                f"(now: {draft['status']}) — nothing was sent")
    if any(field in expect and draft[field] != expect[field]
           for field in CONTENT_FIELDS):
        return ("the draft changed since you reviewed it — reopen it and "
                "send again")
    if "test_mode" in expect and test_mode != expect["test_mode"]:
        return ("the sending mode changed since you confirmed — nothing was "
                "sent; reopen it and confirm again")
    if "recipient_shown" in expect and recipient != expect["recipient_shown"]:
        return ("the recipient changed since you confirmed — nothing was "
                "sent; reopen it and confirm again")
    return ""


def _claim(job_id: int, snapshot: dict, expect: dict | None) -> tuple[str, str, bool]:
    """Atomically move ready/approved → sending. Returns
    (error, recipient, test_mode); error is a user-readable refusal.

    BEGIN IMMEDIATE makes check-then-write atomic across connections, and
    the content comparison (updated_at has second resolution) guarantees
    the message that leaves is exactly the one the worker read.

    The sending mode is resolved HERE, not from the caller's earlier read:
    a real→test flip landing between the gates and the send must not put a
    message in a company's inbox, and a test→real flip must not slip past
    the duplicate-company gate. Everything the mode decides therefore lives
    inside this transaction. The resolved mode is persisted so a claim left
    stuck can never be mistaken for a real application."""
    with db.db() as con:
        con.execute("BEGIN IMMEDIATE")
        current = db.get_draft_by_job(con, job_id)
        if current is None:
            return "the draft disappeared — refresh the queue", "", True
        if current["status"] == "sending":
            return ("a send for this application is already in progress — "
                    "if it is stuck, resolve it from the review queue"), "", True
        if current["status"] == "sent":
            return "this application was already sent", "", True
        if current["status"] not in ("ready", "approved"):
            return (f"the draft is not sendable (status: {current['status']})",
                    "", True)
        recipient, test_mode, error = resolve_recipient(current["recipient"], {
            "real_send_enabled": db.get_setting(con, "real_send_enabled", "0"),
            "test_recipient": db.get_setting(con, "test_recipient", "").strip(),
        })
        if error:
            return error, "", True
        if not gmail.is_plausible_address(recipient):
            return (f"'{recipient}' does not look like a valid e-mail address "
                    f"— fix the recipient"), "", test_mode
        if not test_mode:
            if db.count_outbound_for_draft(con, current["id"]):
                return ("this draft already has a recorded send — check "
                        "Applications"), "", test_mode
            job = db.get_job(con, job_id)
            if job is not None and find_duplicate_bewerbung(
                con, job["company"], recipient
            ) is not None:
                return ("you already applied at this company — see "
                        "Applications before sending again"), "", test_mode
        if any(current[field] != snapshot[field] for field in CONTENT_FIELDS):
            return ("the draft changed while preparing the send — review it "
                    "again"), "", test_mode
        if expect:
            mismatch = expectation_mismatch(expect, current, recipient, test_mode)
            if mismatch:
                return mismatch, "", test_mode
        db.claim_for_send(con, current["id"], test_mode)
        return "", recipient, test_mode


def _release(job_id: int, status: str, error: str = "") -> bool:
    """Return our claimed draft to `status`.

    False when the claim is no longer ours (a human resolved the send while
    it was in flight) — their decision must not be overwritten."""
    with db.db() as con:
        con.execute("BEGIN IMMEDIATE")
        current = db.get_draft_by_job(con, job_id)
        if current is None or current["status"] != "sending":
            log.warning("claim for job %s was resolved elsewhere — not "
                        "releasing it", job_id)
            return False
        db.upsert_draft(con, job_id, {"status": status, "error": error})
        return True


def _mark_uncertain(job_id: int, error: str) -> None:
    """Record why a send is ambiguous, keeping the claim for a human."""
    with db.db() as con:
        con.execute("BEGIN IMMEDIATE")
        current = db.get_draft_by_job(con, job_id)
        if current is None or current["status"] != "sending":
            return
        db.upsert_draft(con, job_id, {"error": error})


def _record_test_send(draft: dict, job: dict, settings: dict, recipient: str,
                      message_id: str, thread_id: str, prev_status: str) -> None:
    with db.db() as con:
        con.execute("BEGIN IMMEDIATE")
        db.add_email_log(con, {
            "direction": EMAIL_OUTBOUND_TEST,
            "gmail_message_id": message_id,
            "gmail_thread_id": thread_id,
            "from_addr": settings["gmail_address"],
            "to_addr": recipient,
            "subject": draft["betreff"],
            "snippet": draft["email_body"][:SNIPPET_CHARS],
            "draft_id": draft["id"],
        })
        # The draft's lifecycle is untouched: a test send consumes nothing.
        # Only restore the status if the claim is still ours.
        current = db.get_draft_by_job(con, job["id"])
        if current is not None and current["status"] == "sending":
            db.upsert_draft(con, job["id"], {"status": prev_status, "error": ""})


def _record_real_send(draft: dict, job: dict, settings: dict, recipient: str,
                      message_id: str, thread_id: str) -> str:
    """Application row + audit log + sent draft in ONE transaction.

    The message is out and Gmail gave us its id — that truth is recorded
    even if a human resolved the claim meanwhile, EXCEPT when they already
    recorded the send themselves: then their row stands and we only
    backfill the Gmail ids they could not know. Returns a note for the
    caller when the normal path was not taken."""
    with db.db() as con:
        con.execute("BEGIN IMMEDIATE")
        current = db.get_draft_by_job(con, job["id"])
        if current is not None and current["status"] == "sent":
            con.execute(
                "UPDATE drafts SET gmail_message_id=?, gmail_thread_id=? "
                "WHERE id=? AND gmail_message_id=''",
                (message_id, thread_id, draft["id"]),
            )
            con.execute(
                "UPDATE email_log SET gmail_message_id=?, gmail_thread_id=? "
                "WHERE draft_id=? AND gmail_message_id IS NULL",
                (message_id, thread_id, draft["id"]),
            )
            return ("this send was already recorded manually — the existing "
                    "application record stands")
        # add_bewerbung, not apply_job: the duplicate gate ran before the
        # claim — after a successful send the record MUST exist regardless.
        bewerbung_id = db.add_bewerbung(con, {
            "gesendet_am": datetime.date.today().isoformat(),
            "firma": job["company"],
            "email": recipient,
            "ansprechpartner": job["ansprechpartner"],
            "strasse": job["contact_strasse"],
            "plz_ort": job["contact_plz_ort"],
            "kanal": "E-Mail",
            "status": "Gesendet",
            "notiz": job["url"],
            "dokument": draft["pdf_path"],
        })
        db.set_job_status(con, job["id"], "applied", bewerbung_id=bewerbung_id)
        db.add_email_log(con, {
            "direction": EMAIL_OUTBOUND,
            "gmail_message_id": message_id,
            "gmail_thread_id": thread_id,
            "from_addr": settings["gmail_address"],
            "to_addr": recipient,
            "subject": draft["betreff"],
            "snippet": draft["email_body"][:SNIPPET_CHARS],
            "draft_id": draft["id"],
            "bewerbung_id": bewerbung_id,
        })
        db.record_send(con, draft["id"], message_id, thread_id, bewerbung_id)
        return ""


def _refreshed(job_id: int) -> dict | None:
    with db.db() as con:
        row = db.get_draft_by_job(con, job_id)
        return dict(row) if row is not None else None


def _send_draft(job_id: int, expect: dict | None = None) -> dict:
    """Synchronous worker — gates, claim, Gmail call, one recording write."""
    draft, job, settings = _load_context(job_id)
    if job is None:
        return _error("posting not found")
    if draft is None:
        return _error("draft the application first")
    if draft["status"] == "sent":
        return _error("this application was already sent")
    if draft["status"] == "sending":
        return _error("a send for this application is already in progress — "
                      "if it is stuck, resolve it from the review queue")
    if draft["status"] not in ("ready", "approved"):
        return _error(f"the draft is not sendable (status: {draft['status']})")
    if not draft["betreff"].strip() or not draft["email_body"].strip():
        return _error("the draft has no Betreff or e-mail text — re-draft it")
    if not draft["anschreiben_body"].strip():
        return _error("the draft has no Anschreiben — the letter page would "
                      "go out empty; re-draft it")
    if not draft["pdf_path"]:
        return _error("create the Bewerbungsmappe PDF first — a German "
                      "application is sent as ONE merged PDF")
    attachment = pathlib.Path(draft["pdf_path"])
    if not attachment.is_file():
        return _error("the Mappe file is gone — create the PDF again")

    # Early gates: cheap, user-facing feedback. The claim below re-derives
    # the mode and recipient authoritatively — these must never be the
    # values a message is actually sent with.
    preview_recipient, preview_test_mode, recipient_error = resolve_recipient(
        draft["recipient"], settings
    )
    if recipient_error:
        return _error(recipient_error, kind="global")
    if not gmail.is_plausible_address(preview_recipient):
        return _error(f"'{preview_recipient}' does not look like a valid "
                      f"e-mail address — fix the recipient",
                      kind="global" if preview_test_mode else "draft")
    if not preview_test_mode:
        with db.db() as con:
            dup = find_duplicate_bewerbung(con, job["company"],
                                           preview_recipient)
        if dup is not None:
            return _error("you already applied at this company — see "
                          "Applications before sending again")
    cap = int(settings["daily_send_cap"] or "15")
    if settings["sent_today"] >= cap:
        return _error(f"daily send cap reached ({settings['sent_today']}/{cap})"
                      f" — sending continues tomorrow, or raise the cap in "
                      f"Settings", kind="global")
    if not gmail.is_connected():
        return _error("Gmail is not connected — use Connect Gmail in Settings",
                      kind="global")

    prev_status = draft["status"]
    claim_error, recipient, test_mode = _claim(job_id, draft, expect)
    if claim_error:
        return _error(claim_error)

    try:
        message = gmail.build_mime(
            to=recipient,
            subject=draft["betreff"],
            text_body=draft["email_body"],
            from_name=settings["applicant_name"],
            from_addr=settings["gmail_address"],
            attachment=attachment,
        )
        message_id, thread_id = gmail.send_message(message)
    except gmail.GmailUncertain as exc:
        # The message may already be in the recipient's inbox: releasing the
        # claim would invite a second copy. Keep it for human resolution.
        _mark_uncertain(job_id, f"send outcome unknown: {exc}")
        log.warning("ambiguous send for job %s: %s", job_id, exc)
        return _error(f"the send may or may not have gone out ({exc}) — check "
                      f"the Gmail 'Sent' folder and resolve it in the queue")
    except gmail.GmailError as exc:
        # Gmail answered (refusal) or we never had a connection: not sent.
        _release(job_id, prev_status, error=f"send failed: {exc}")
        return _error(f"send failed: {exc}")
    except Exception as exc:
        # Unexpected failure before the message left (a broken attachment, a
        # malformed From): release so the user can retry, then surface it.
        _release(job_id, prev_status, error=f"send failed unexpectedly: {exc}")
        raise

    try:
        if test_mode:
            _record_test_send(draft, job, settings, recipient,
                              message_id, thread_id, prev_status)
            note = ""
        else:
            note = _record_real_send(draft, job, settings, recipient,
                                     message_id, thread_id)
    except Exception:
        # The e-mail IS out but the books don't say so. Fail loud and leave
        # the draft in 'sending' — the queue's manual resolution (a human
        # checking the Gmail Sent folder) is the only safe recovery.
        log.critical(
            "sent gmail message %s for job %s but recording failed — "
            "resolve the draft from the review queue", message_id, job_id,
        )
        raise
    return {"ok": True, "error": note, "kind": "", "test_mode": test_mode,
            "recipient": recipient, "draft": _refreshed(job_id)}


async def send_draft(job_id: int, expect: dict | None = None) -> dict:
    """Send one draft (Approve & Send / auto-send).

    `expect` pins what the caller believes it is sending: any of the
    CONTENT_FIELDS, "status", "test_mode" and "recipient_shown". A
    mismatch refuses the send — an approval that no longer describes the
    message is not an approval.

    Returns {"ok", "error", "kind", "test_mode", "recipient", "draft"};
    error is a user-readable reason when ok is False."""
    async with _lock:
        return await asyncio.to_thread(_send_draft, job_id, expect)


# --------------------------------------------------------------------------
# Queue lifecycle transitions (quick DB ops — UI calls them via run.io_bound)
# --------------------------------------------------------------------------
def _transition(job_id: int, target: str, allowed_from: tuple[str, ...],
                error_note: str = "") -> dict:
    with db.db() as con:
        con.execute("BEGIN IMMEDIATE")
        draft = db.get_draft_by_job(con, job_id)
        if draft is None:
            return _error("no draft for this posting")
        if draft["status"] not in allowed_from:
            return _error(
                f"cannot move a draft from '{draft['status']}' to '{target}'"
            )
        db.upsert_draft(con, job_id, {"status": target, "error": error_note})
        row = db.get_draft_by_job(con, job_id)
        return {"ok": True, "error": "", "kind": "", "test_mode": False,
                "recipient": "", "draft": dict(row)}


def approve(job_id: int) -> dict:
    """Queue a ready draft for auto-send. Same material gates as sending —
    an approved draft must be sendable without further human input."""
    with db.db() as con:
        draft = db.get_draft_by_job(con, job_id)
    if draft is not None and draft["status"] == "ready":
        if not draft["betreff"].strip() or not draft["email_body"].strip():
            return _error("the draft has no Betreff or e-mail text — re-draft it")
        if not draft["anschreiben_body"].strip():
            return _error("the draft has no Anschreiben — the letter page "
                          "would go out empty; re-draft it")
        if not draft["pdf_path"] or not pathlib.Path(draft["pdf_path"]).is_file():
            return _error("create the Bewerbungsmappe PDF before approving")
    return _transition(job_id, "approved", ("ready",))


def unapprove(job_id: int) -> dict:
    return _transition(job_id, "ready", ("approved",))


def demote_failed_autosend(job_id: int, error: str) -> dict:
    """A failed auto-send returns the draft to human attention instead of
    retrying unattended — back to ready with the reason recorded."""
    return _transition(job_id, "ready", ("approved",),
                       error_note=f"auto-send failed: {error}")


def discard(job_id: int) -> dict:
    return _transition(job_id, "discarded", ("ready", "approved", "failed"))


def restore(job_id: int) -> dict:
    return _transition(job_id, "ready", ("discarded",))


def resolve_sending(job_id: int, assume_sent: bool) -> dict:
    """Human resolution for a draft stuck in 'sending' (process died between
    the Gmail call and the recording write).

    The user checks the Gmail Sent folder first: 'assume_sent' records the
    application without Gmail ids; otherwise the draft returns to ready.

    A stuck TEST send is refused for assume_sent: it really is in the Sent
    folder (it went to the test inbox), so a user following the dialog's
    instruction literally would otherwise fabricate a record of applying to
    a company they never contacted — and that record would then block the
    real application forever."""
    if not assume_sent:
        return _transition(
            job_id, "ready", ("sending",),
            error_note="send resolved manually: assumed not sent",
        )
    with db.db() as con:
        con.execute("BEGIN IMMEDIATE")
        draft = db.get_draft_by_job(con, job_id)
        job = db.get_job(con, job_id)
        if draft is None or job is None:
            return _error("no draft for this posting")
        if draft["status"] != "sending":
            return _error(f"nothing to resolve (status: {draft['status']})")
        if draft["sending_test"]:
            return _error(
                "this was a TEST send — it went to your test inbox, not to "
                "the company, so it is not an application. Resolve it as "
                "'not sent'."
            )
        bewerbung_id = db.add_bewerbung(con, {
            "gesendet_am": datetime.date.today().isoformat(),
            "firma": job["company"],
            "email": draft["recipient"],
            "ansprechpartner": job["ansprechpartner"],
            "strasse": job["contact_strasse"],
            "plz_ort": job["contact_plz_ort"],
            "kanal": "E-Mail",
            "status": "Gesendet",
            "notiz": f"{job['url']} | send resolved manually: assumed sent",
            "dokument": draft["pdf_path"],
        })
        db.set_job_status(con, job_id, "applied", bewerbung_id=bewerbung_id)
        db.add_email_log(con, {
            "direction": EMAIL_OUTBOUND,
            "gmail_message_id": None,  # unknown — the recording write died
            "from_addr": db.get_setting(con, "gmail_address", ""),
            "to_addr": draft["recipient"],
            "subject": draft["betreff"],
            "snippet": draft["email_body"][:SNIPPET_CHARS],
            "draft_id": draft["id"],
            "bewerbung_id": bewerbung_id,
            "matched_by": "manual_resolution",
        })
        db.record_send(con, draft["id"], "", "", bewerbung_id)
        row = db.get_draft_by_job(con, job_id)
        return {"ok": True, "error": "", "kind": "", "test_mode": False,
                "recipient": draft["recipient"], "draft": dict(row)}
