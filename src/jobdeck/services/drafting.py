"""On-demand application drafting for a single posting.

User-triggered only (the Draft button) — never scheduled, so the spend is
one metered LLM call per click. Gate order mirrors scoring: master AI
toggle, API key, profile.md, plus the applicant name that the code-built
Betreff needs. A 'generating' draft row acts as an optimistic claim so a
double-click cannot pay for the same posting twice; a claim older than
CLAIM_TIMEOUT_MIN is treated as abandoned (process died mid-call).

Regeneration never touches a draft that is committed to the send path:
'sending' is the evidence a stuck send leaves behind (only the review
queue may resolve it), and rewriting an 'approved' or 'sent' draft would
throw away the user's approval or falsify the record of what went out.
"""

import asyncio
import datetime
import logging

from jobdeck import config, db
from jobdeck.ai import drafting as ai_drafting
from jobdeck.ai import llm, profile
from jobdeck.ai.drafting import resolve_refnr  # noqa: F401 — re-exported
from jobdeck.services import upload

log = logging.getLogger(__name__)

CLAIM_TIMEOUT_MIN = 15

# Statuses a regeneration must refuse, with the way out for each.
# German, because every one of these is shown to the user verbatim: the job
# inbox and the Postausgang both do `say(result["error"])`. They were English
# on German screens, and the one added with 'filed' was broken English at that.
NO_REGEN = {
    "approved": "Dieser Entwurf ist für den Auto-Versand freigegeben — die "
                "Freigabe im Postausgang zurücknehmen, dann neu schreiben.",
    "sending": "Für diese Anzeige läuft ein Versand, oder er ist stecken "
               "geblieben — erst im Postausgang auflösen.",
    "sent": "Diese Bewerbung ist schon raus — neu schreiben würde die "
            "Aufzeichnung dessen überschreiben, was gesendet wurde.",
    "filed": "Zu dieser Anzeige ist eine Bewerbung eingetragen, also ist das "
             "Anschreiben abgelegt. Wenn das nicht stimmt, erst die Bewerbung "
             "im Register löschen.",
}


def claim_age_minutes(updated_at: object) -> float:
    """How long a 'generating' row has been claimed, 0.0 when unreadable.

    A stored timestamp we cannot parse is not evidence that anything is
    wrong, so it reads as fresh: treating it as abandoned would let a second
    claim start while the first is still spending money."""
    try:
        started = datetime.datetime.fromisoformat(str(updated_at))
    except (TypeError, ValueError):
        return 0.0
    return (datetime.datetime.now() - started).total_seconds() / 60


def claim_is_stale(updated_at: object) -> bool:
    """Has the process holding this claim died?

    ONE definition for the three places that must agree: the reclaim in
    `_claim`, the review queue's wording, and the Job inbox's Draft button —
    which is the only surface that can trigger the reclaim, so a button that
    hid for longer than this made an abandoned draft unrecoverable."""
    return claim_age_minutes(updated_at) >= CLAIM_TIMEOUT_MIN


def _error(message: str) -> dict:
    return {"ok": False, "error": message, "draft": None}


def _ai_enabled() -> bool:
    with db.db() as con:
        return db.ai_enabled(con)


def _applicant_name() -> str:
    with db.db() as con:
        return db.get_setting(con, "applicant_name", "").strip()


def _email_signature() -> str:
    with db.db() as con:
        return db.get_setting(con, "email_signature", "")


def _get_job(job_id: int):
    with db.db() as con:
        return db.get_job(con, job_id)


def _claim(job_id: int) -> str:
    """Mark the job's draft as 'generating'; '' on success, else the refusal.

    BEGIN IMMEDIATE makes the check-then-write atomic across connections:
    a concurrent second claim blocks on the write lock, then sees the
    first claim's 'generating' row and backs off — no double spend."""
    with db.db() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = db.get_draft_by_job(con, job_id)
        if existing is not None:
            refusal = NO_REGEN.get(existing["status"])
            if refusal:
                return refusal
            if existing["status"] == "generating":
                if not claim_is_stale(existing["updated_at"]):
                    return "a draft for this posting is already being generated"
                log.warning("reclaiming abandoned draft for job %s", job_id)
        # The daily quota is enforced HERE, inside the same transaction that
        # commits the spend — a screen that only greys out a button is a screen
        # the keyboard, the batch and a second tab all walk past.
        cap = db.daily_draft_cap(con)
        written = db.count_drafts_today(con)
        if written >= cap:
            return (f"today's letter limit is used up ({written}/{cap}) — "
                    f"raise it in Einstellungen")
        db.note_draft_written(con)
        # A regenerated draft invalidates any previously built Mappe — the
        # PDF on disk still holds the OLD Anschreiben.
        db.upsert_draft(con, job_id, {"status": "generating", "pdf_path": ""})
        # And the package built from the OLD letter leaves circulation with
        # it: a "⧉ Anschreiben" chip must never offer the text he just
        # replaced. The strip reads "Mappe wird gebaut …" until the next
        # build lands.
        upload.withdraw(con, job_id)
        return ""


def _previous_letters(job_id: int) -> list[str]:
    """The letters already written for OTHER postings, so a new one is not
    the same letter — and a redraft is not compared with itself."""
    with db.db() as con:
        return db.recent_letter_bodies(con, exclude_job_id=job_id)


def _finish(job_id: int, values: dict, usage: llm.LLMResult | None) -> dict | None:
    """Persist the generation result, unless the claim is no longer ours.

    Metering happens either way: those tokens were paid for."""
    with db.db() as con:
        con.execute("BEGIN IMMEDIATE")
        current = db.get_draft_by_job(con, job_id)
        if usage is not None:
            db.record_llm_usage(
                con, usage.input_tokens, usage.output_tokens, usage.cost_usd
            )
        if current is None or current["status"] != "generating":
            # Something moved the draft out from under this generation —
            # never stomp the newer state with a stale result.
            log.warning("draft for job %s changed while generating (now %s) "
                        "— discarding the generated text", job_id,
                        current["status"] if current else "gone")
            return None
        draft_id = db.upsert_draft(con, job_id, values)
        row = db.get_draft(con, draft_id)
        return dict(row) if row is not None else None


async def draft_for_job(job_id: int) -> dict:
    """Draft Anschreiben + e-mail for one posting.

    Returns {"ok": bool, "error": str, "draft": dict | None}; error is a
    user-readable reason when ok is False."""
    if not await asyncio.to_thread(_ai_enabled):
        return _error("AI is disabled — enable the switch in Settings first")
    if not config.anthropic_api_key():
        return _error("ANTHROPIC_API_KEY is not set")
    profile_text = await asyncio.to_thread(profile.load_profile)
    if not profile_text:
        return _error(f"create {config.PROFILE_PATH} first")
    applicant_name = await asyncio.to_thread(_applicant_name)
    if not applicant_name:
        return _error("set your applicant name in Settings first")
    job = await asyncio.to_thread(_get_job, job_id)
    if job is None:
        return _error("posting not found")
    claim_error = await asyncio.to_thread(_claim, job_id)
    if claim_error:
        return _error(claim_error)

    refnr = resolve_refnr(job)
    previous = await asyncio.to_thread(_previous_letters, job_id)
    try:
        anschreiben, email_body, stellenbezeichnung, usage = await asyncio.to_thread(
            ai_drafting.draft_application, job, profile_text, refnr,
            applicant_name, previous,
        )
    except llm.LLMNotConfigured as exc:
        await asyncio.to_thread(
            _finish, job_id, {"status": "failed", "error": str(exc)}, None
        )
        return _error(str(exc))
    except llm.LLMError as exc:
        await asyncio.to_thread(
            _finish, job_id, {"status": "failed", "error": str(exc)}, exc.usage
        )
        log.warning("drafting job %s failed: %s", job_id, exc)
        return _error(f"drafting failed: {exc}")
    except Exception as exc:
        # Unexpected failure: release the claim so the user can retry
        # immediately, then surface the error — never swallow it.
        await asyncio.to_thread(
            _finish, job_id, {"status": "failed", "error": f"unexpected: {exc}"}, None
        )
        raise

    # The LLM's clean Stellenbezeichnung feeds the Betreff (falling back to the
    # raw title); build_betreff injects the verified Refnr + name.
    betreff = ai_drafting.build_betreff(
        ai_drafting.align_gender_marker(stellenbezeichnung or job["title"],
                                        job["title"]),
        refnr, applicant_name,
    )
    signature = await asyncio.to_thread(_email_signature)
    draft = await asyncio.to_thread(
        _finish,
        job_id,
        {
            "status": "ready",
            "recipient": job["contact_email"] or "",
            "betreff": betreff,
            "email_body": ai_drafting.append_signature(email_body, signature),
            "anschreiben_body": anschreiben,
            "llm_model": usage.model,
        },
        usage,
    )
    if draft is None:
        return _error("the draft changed while it was being generated — "
                      "check the Postausgang")
    return {"ok": True, "error": "", "draft": draft}
