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

log = logging.getLogger(__name__)

CLAIM_TIMEOUT_MIN = 15

# Statuses a regeneration must refuse, with the way out for each.
NO_REGEN = {
    "approved": "this draft is approved for sending — return it to ready in "
                "the review queue before re-drafting",
    "sending": "a send for this posting is in progress or stuck — resolve it "
               "in the review queue before re-drafting",
    "sent": "this application was already sent — re-drafting would rewrite "
            "the record of what went out",
}


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
                started = datetime.datetime.fromisoformat(existing["updated_at"])
                age_min = (datetime.datetime.now() - started).total_seconds() / 60
                if age_min < CLAIM_TIMEOUT_MIN:
                    return "a draft for this posting is already being generated"
                log.warning("reclaiming abandoned draft for job %s", job_id)
        # A regenerated draft invalidates any previously built Mappe — the
        # PDF on disk still holds the OLD Anschreiben.
        db.upsert_draft(con, job_id, {"status": "generating", "pdf_path": ""})
        return ""


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


def resolve_refnr(job) -> str:
    """Extracted Referenznummer first; Arbeitsagentur ids ARE the Refnr."""
    if (job["refnr"] or "").strip():
        return job["refnr"].strip()
    if job["source"] == "arbeitsagentur":
        return job["external_id"]
    return ""


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
    try:
        anschreiben, email_body, stellenbezeichnung, usage = await asyncio.to_thread(
            ai_drafting.draft_application, job, profile_text, refnr, applicant_name
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
        stellenbezeichnung or job["title"], refnr, applicant_name
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
                      "check the review queue")
    return {"ok": True, "error": "", "draft": draft}
