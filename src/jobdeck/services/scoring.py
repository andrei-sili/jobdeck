"""Background match scoring for newly discovered jobs.

Runs on the scheduler alongside polling: picks unscored 'new' postings, asks
the LLM for a 0-100 fit against the user's profile and stores score + reason
(the inbox already sorts by score). Skips quietly — with a log hint — while
the AI toggle is off (the default) or no API key or profile.md exists; a
failing posting is logged and does not block the rest of the batch.

Cost guards: a module-level lock makes runs single-flight (the settings-page
button would otherwise overlap the scheduled run and pay for the same jobs
twice), failed calls are still metered, and a posting that keeps failing is
retried at most MAX_ATTEMPTS times per process instead of forever.
"""

import asyncio
import logging

from jobdeck import config, db
from jobdeck.ai import llm, profile
from jobdeck.ai import scoring as ai_scoring

log = logging.getLogger(__name__)

BATCH_LIMIT = 20  # per run; the interval job drains any backlog over time
MAX_ATTEMPTS = 3  # per process — an app restart re-enables given-up jobs

_lock = asyncio.Lock()
_attempts: dict[int, int] = {}  # job id -> failed scoring attempts


def _ai_enabled() -> bool:
    with db.db() as con:
        return db.ai_enabled(con)


def _unscored_jobs(limit: int, exclude_ids: set[int]):
    with db.db() as con:
        return db.list_unscored_jobs(con, limit, exclude_ids=exclude_ids)


def _profiles_by_id():
    with db.db() as con:
        return {row["id"]: dict(row) for row in db.list_profiles(con)}


def _global_hard_tags() -> str:
    """Requirements that hold for every search, whatever the profile."""
    with db.db() as con:
        return db.get_setting(con, "global_hard_tags", "")


def _persist_score(
    job_id: int, score: int, reason: str, contacts: dict, usage: llm.LLMResult
) -> None:
    with db.db() as con:
        db.set_job_score(con, job_id, score, reason)
        db.set_job_contacts(con, job_id, contacts)
        db.record_llm_usage(con, usage.input_tokens, usage.output_tokens, usage.cost_usd)


def _persist_usage(usage: llm.LLMResult) -> None:
    with db.db() as con:
        db.record_llm_usage(con, usage.input_tokens, usage.output_tokens, usage.cost_usd)


async def score_new_jobs(limit: int = BATCH_LIMIT) -> dict[str, int]:
    """Score up to `limit` unscored new jobs. Returns outcome counters."""
    counters = {"scored": 0, "failed": 0}
    if not await asyncio.to_thread(_ai_enabled):
        log.info("scoring skipped: AI is disabled in Settings")
        return counters
    if not config.anthropic_api_key():
        log.info("scoring skipped: ANTHROPIC_API_KEY not set")
        return counters
    profile_text = await asyncio.to_thread(profile.load_profile)
    if not profile_text:
        log.info("scoring skipped: create %s first", config.PROFILE_PATH)
        return counters

    async with _lock:  # manual runs and the scheduled job never overlap
        given_up = {job_id for job_id, n in _attempts.items() if n >= MAX_ATTEMPTS}
        jobs = await asyncio.to_thread(_unscored_jobs, limit, given_up)
        # One criteria snapshot per batch: a mid-batch profile edit applies
        # from the next run — deliberate, keeps a batch internally consistent.
        profiles = await asyncio.to_thread(_profiles_by_id) if jobs else {}
        global_tags = await asyncio.to_thread(_global_hard_tags) if jobs else ""
        for job in jobs:
            # Re-check the kill switch before every paid call: flipping it
            # off mid-batch (or while queued behind the lock) must stop the
            # spend now, not after up to BATCH_LIMIT more calls.
            if not await asyncio.to_thread(_ai_enabled):
                log.info("scoring stopped: AI was disabled mid-run")
                break
            # deleted profile (profile_id NULL) → generic scoring, no criteria
            criteria = ai_scoring.criteria_from_profile(
                profiles.get(job["profile_id"]), global_tags
            )
            try:
                score, reason, contacts, usage = await asyncio.to_thread(
                    ai_scoring.score_job, job, profile_text, criteria
                )
            except llm.LLMNotConfigured:
                break
            except llm.LLMError as exc:
                counters["failed"] += 1
                _attempts[job["id"]] = _attempts.get(job["id"], 0) + 1
                if exc.usage is not None:  # the failed call still cost tokens
                    await asyncio.to_thread(_persist_usage, exc.usage)
                if _attempts[job["id"]] >= MAX_ATTEMPTS:
                    log.warning(
                        "scoring job %s failed %d times, giving up until restart: %s",
                        job["id"], MAX_ATTEMPTS, exc,
                    )
                else:
                    log.warning("scoring job %s failed: %s", job["id"], exc)
                continue
            _attempts.pop(job["id"], None)
            await asyncio.to_thread(
                _persist_score, job["id"], score, reason, contacts, usage
            )
            counters["scored"] += 1

    if counters["scored"] or counters["failed"]:
        log.info("scoring: %d scored, %d failed", counters["scored"], counters["failed"])
    return counters
