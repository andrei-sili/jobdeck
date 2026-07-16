"""On-demand application drafting for a single posting.

User-triggered only (the Draft button) — never scheduled, so the spend is
one metered LLM call per click. Gate order mirrors scoring: master AI
toggle, API key, profile.md, plus the applicant name that the code-built
Betreff needs. A 'generating' draft row acts as an optimistic claim so a
double-click cannot pay for the same posting twice; a claim older than
CLAIM_TIMEOUT_MIN is treated as abandoned (process died mid-call).
"""

import asyncio
import datetime
import logging

from jobdeck import config, db
from jobdeck.ai import drafting as ai_drafting
from jobdeck.ai import llm, profile

log = logging.getLogger(__name__)

CLAIM_TIMEOUT_MIN = 15


def _error(message: str) -> dict:
    return {"ok": False, "error": message, "draft": None}


def _ai_enabled() -> bool:
    with db.db() as con:
        return db.ai_enabled(con)


def _applicant_name() -> str:
    with db.db() as con:
        return db.get_setting(con, "applicant_name", "").strip()


def _get_job(job_id: int):
    with db.db() as con:
        return db.get_job(con, job_id)


def _claim(job_id: int) -> bool:
    """Mark the job's draft as 'generating'; False if already claimed.

    BEGIN IMMEDIATE makes the check-then-write atomic across connections:
    a concurrent second claim blocks on the write lock, then sees the
    first claim's 'generating' row and backs off — no double spend."""
    with db.db() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = db.get_draft_by_job(con, job_id)
        if existing is not None and existing["status"] == "generating":
            started = datetime.datetime.fromisoformat(existing["updated_at"])
            age_min = (datetime.datetime.now() - started).total_seconds() / 60
            if age_min < CLAIM_TIMEOUT_MIN:
                return False
            log.warning("reclaiming abandoned draft for job %s", job_id)
        db.upsert_draft(con, job_id, {"status": "generating"})
        return True


def _finish(job_id: int, values: dict, usage: llm.LLMResult | None) -> dict | None:
    with db.db() as con:
        draft_id = db.upsert_draft(con, job_id, values)
        if usage is not None:
            db.record_llm_usage(
                con, usage.input_tokens, usage.output_tokens, usage.cost_usd
            )
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
    if not await asyncio.to_thread(_claim, job_id):
        return _error("a draft for this posting is already being generated")

    refnr = resolve_refnr(job)
    try:
        anschreiben, email_body, usage = await asyncio.to_thread(
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

    betreff = ai_drafting.build_betreff(job["title"], refnr, applicant_name)
    draft = await asyncio.to_thread(
        _finish,
        job_id,
        {
            "status": "ready",
            "recipient": job["contact_email"] or "",
            "betreff": betreff,
            "email_body": email_body,
            "anschreiben_body": anschreiben,
            "llm_model": usage.model,
        },
        usage,
    )
    return {"ok": True, "error": "", "draft": draft}
