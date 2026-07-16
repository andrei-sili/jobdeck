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
review queue.
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


def _error(message: str) -> dict:
    return {"ok": False, "error": message, "test_mode": False,
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


def _claim(job_id: int, snapshot: dict, test_mode: bool) -> str:
    """Atomically move ready/approved → sending; '' on success, else the
    user-readable refusal.

    BEGIN IMMEDIATE makes check-then-write atomic across connections, and
    the content comparison (updated_at has second resolution) guarantees
    the message that leaves is exactly the one the user approved."""
    with db.db() as con:
        con.execute("BEGIN IMMEDIATE")
        current = db.get_draft_by_job(con, job_id)
        if current is None:
            return "the draft disappeared — refresh the queue"
        if current["status"] == "sending":
            return ("a send for this application is already in progress — "
                    "if it is stuck, resolve it from the review queue")
        if current["status"] == "sent":
            return "this application was already sent"
        if current["status"] not in ("ready", "approved"):
            return f"the draft is not sendable (status: {current['status']})"
        if not test_mode and db.count_outbound_for_draft(con, current["id"]):
            return "this draft already has a recorded send — check Applications"
        if any(
            current[field] != snapshot[field]
            for field in ("updated_at", "recipient", "betreff",
                          "email_body", "pdf_path")
        ):
            return "the draft changed while preparing the send — review it again"
        db.upsert_draft(con, job_id, {"status": "sending"})
        return ""


def _release(job_id: int, status: str, error: str = "") -> None:
    with db.db() as con:
        db.upsert_draft(con, job_id, {"status": status, "error": error})


def _record_test_send(draft: dict, job: dict, settings: dict, recipient: str,
                      message_id: str, thread_id: str, prev_status: str) -> None:
    with db.db() as con:
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
        db.upsert_draft(con, job["id"], {"status": prev_status, "error": ""})


def _record_real_send(draft: dict, job: dict, settings: dict, recipient: str,
                      message_id: str, thread_id: str) -> None:
    """Application row + audit log + sent draft in ONE transaction."""
    with db.db() as con:
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


def _refreshed(job_id: int) -> dict | None:
    with db.db() as con:
        row = db.get_draft_by_job(con, job_id)
        return dict(row) if row is not None else None


def _send_draft(job_id: int) -> dict:
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
    if not draft["pdf_path"]:
        return _error("create the Bewerbungsmappe PDF first — a German "
                      "application is sent as ONE merged PDF")
    attachment = pathlib.Path(draft["pdf_path"])
    if not attachment.is_file():
        return _error("the Mappe file is gone — create the PDF again")

    recipient, test_mode, recipient_error = resolve_recipient(
        draft["recipient"], settings
    )
    if recipient_error:
        return _error(recipient_error)
    if not gmail.is_plausible_address(recipient):
        return _error(f"'{recipient}' does not look like a valid e-mail "
                      f"address — fix the recipient")
    if not test_mode:
        with db.db() as con:
            dup = find_duplicate_bewerbung(con, job["company"], recipient)
        if dup is not None:
            return _error("you already applied at this company — see "
                          "Applications before sending again")
    cap = int(settings["daily_send_cap"] or "15")
    if settings["sent_today"] >= cap:
        return _error(f"daily send cap reached ({settings['sent_today']}/{cap})"
                      f" — sending continues tomorrow, or raise the cap in "
                      f"Settings")
    if not gmail.is_connected():
        return _error("Gmail is not connected — use Connect Gmail in Settings")

    prev_status = draft["status"]
    claim_error = _claim(job_id, draft, test_mode)
    if claim_error:
        return _error(claim_error)

    message = gmail.build_mime(
        to=recipient,
        subject=draft["betreff"],
        text_body=draft["email_body"],
        from_name=settings["applicant_name"],
        from_addr=settings["gmail_address"],
        attachment=attachment,
    )
    try:
        message_id, thread_id = gmail.send_message(message)
    except gmail.GmailError as exc:
        _release(job_id, prev_status, error=f"send failed: {exc}")
        return _error(f"send failed: {exc}")
    except Exception as exc:
        # Unexpected failure between claim and send: release so the user can
        # retry, then surface it — never swallow, never strand the draft.
        _release(job_id, prev_status, error=f"send failed unexpectedly: {exc}")
        raise

    try:
        if test_mode:
            _record_test_send(draft, job, settings, recipient,
                              message_id, thread_id, prev_status)
        else:
            _record_real_send(draft, job, settings, recipient,
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
    return {"ok": True, "error": "", "test_mode": test_mode,
            "recipient": recipient, "draft": _refreshed(job_id)}


async def send_draft(job_id: int) -> dict:
    """Send one draft (Approve & Send / auto-send).

    Returns {"ok", "error", "test_mode", "recipient", "draft"}; error is a
    user-readable reason when ok is False."""
    async with _lock:
        return await asyncio.to_thread(_send_draft, job_id)


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
        return {"ok": True, "error": "", "test_mode": False,
                "recipient": "", "draft": dict(row)}


def approve(job_id: int) -> dict:
    """Queue a ready draft for auto-send. Same material gates as sending —
    an approved draft must be sendable without further human input."""
    with db.db() as con:
        draft = db.get_draft_by_job(con, job_id)
    if draft is not None and draft["status"] == "ready":
        if not draft["betreff"].strip() or not draft["email_body"].strip():
            return _error("the draft has no Betreff or e-mail text — re-draft it")
        if not draft["pdf_path"] or not pathlib.Path(draft["pdf_path"]).is_file():
            return _error("create the Bewerbungsmappe PDF before approving")
    return _transition(job_id, "approved", ("ready",))


def unapprove(job_id: int) -> dict:
    return _transition(job_id, "ready", ("approved",))


def discard(job_id: int) -> dict:
    return _transition(job_id, "discarded", ("ready", "approved", "failed"))


def restore(job_id: int) -> dict:
    return _transition(job_id, "ready", ("discarded",))


def resolve_sending(job_id: int, assume_sent: bool) -> dict:
    """Human resolution for a draft stuck in 'sending' (process died between
    the Gmail call and the recording write).

    The user checks the Gmail Sent folder first: 'assume_sent' records the
    application without Gmail ids; otherwise the draft returns to ready.
    A stuck TEST send should always be resolved as not sent — no records
    were going to be written for it anyway."""
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
        return {"ok": True, "error": "", "test_mode": False,
                "recipient": draft["recipient"], "draft": dict(row)}
