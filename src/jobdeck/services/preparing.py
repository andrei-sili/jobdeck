"""Fill the review queue with a day's worth of applications, in one press.

His question, 2026-08-10: "why doesn't the Review queue show me 5 current
postings with the best score, ready to send?" It could not — the queue shows
DRAFTS, and a draft only exists once someone presses "Draft application" on one
row at a time. Between "nothing is automatic" and "one click per posting" there
was nothing, exactly as with the apply-channel backlog before `resolve_pending`.

This is that missing step: pick the best-ranked postings that pass his filter,
write the Anschreiben and build the Bewerbungsmappe for each, and stop at the
day's number. It PREPARES only — sending stays a deliberate click per draft, and
the daily send cap is untouched.

Money is the reason it is not a scheduler job: every draft is a paid model call
(~$0.09 on Sonnet), so this runs when he asks, reports what it spent, and never
prepares more than the day's quota however often he presses it.
"""

import asyncio
import logging

from jobdeck import config, db
from jobdeck.services import drafting, mappe

log = logging.getLogger(__name__)

# His filter, agreed 2026-08-10. Settings override every one of them.
DEFAULT_MAX_AGE_DAYS = 21   # 21d/70 gave 35 candidates — a week of runway
DEFAULT_MIN_SCORE = 70      # below this the letter goes to a mediocre fit
DEFAULT_PER_DAY = 5         # the send cap, so the queue shows exactly today's work
DEFAULT_INCLUDE_FORMS = True  # only 4 candidates carry an e-mail; the rest are forms

# What one prepared application costs, measured over his real drafts (Sonnet,
# ~$0.06-0.09 each). Used ONLY to show an estimate before he presses; the real
# figure comes from the metering afterwards.
COST_PER_DRAFT_USD = 0.09

BATCH_PAUSE_S = 1.0  # between postings: a paid model call, not a race

_lock = asyncio.Lock()


def settings(con) -> dict:
    """His filter as stored, with the agreed defaults when unset."""
    def number(key: str, default: int) -> int:
        raw = (db.get_setting(con, key, "") or "").strip()
        try:
            return max(0, int(float(raw)))
        except (ValueError, OverflowError):
            # OverflowError, not just ValueError: float("1e999") is inf, and
            # int(inf) raises — the same shape that silently killed Create PDF
            # in July when a budget setting was hand-edited to infinity
            return default

    return {
        "max_age_days": number("prepare_max_age_days", DEFAULT_MAX_AGE_DAYS),
        "min_score": number("prepare_min_score", DEFAULT_MIN_SCORE),
        "per_day": number("prepare_per_day", DEFAULT_PER_DAY),
        "include_forms": db.get_setting(
            con, "prepare_include_forms", "1" if DEFAULT_INCLUDE_FORMS else "0"
        ) != "0",
    }


def _plan(limit: int | None) -> dict:
    """What a run would do, without doing any of it."""
    with db.db() as con:
        cfg = settings(con)
        waiting = db.count_waiting_drafts(con)
        done_today = db.count_drafts_created_today(con)
        # top the QUEUE up to the day's number: what he asked to see is a queue
        # holding N applications, so a draft he discarded or sent frees its place
        room = max(0, cfg["per_day"] - waiting)
        if limit is not None:
            room = min(room, limit)
        candidates = [dict(r) for r in db.jobs_to_prepare(
            con, room, cfg["max_age_days"], cfg["min_score"], cfg["include_forms"]
        )] if room else []
    return {"config": cfg, "waiting": waiting, "done_today": done_today,
            "room": room,
            "candidates": candidates,
            "estimate_usd": round(len(candidates) * COST_PER_DRAFT_USD, 2)}


def plan(limit: int | None = None) -> dict:
    """The estimate to show BEFORE spending anything."""
    return _plan(limit)


async def prepare_day(limit: int | None = None) -> dict:
    """Draft and build the Mappe for today's best candidates.

    Never raises: one posting that fails to draft is counted and the pass goes
    on. Single-flight, because two overlapping runs would pay twice for the same
    posting. `limit` caps a single press below the daily quota; the quota itself
    is what stops repeated presses.
    """
    empty = {"prepared": 0, "failed": 0, "no_mappe": 0, "candidates": 0,
             "waiting": 0, "done_today": 0, "room": 0, "cost_usd": 0.0,
             "errors": []}
    if _lock.locked():
        log.info("preparing: a run is already in progress — skipping this one")
        return {**empty, "skipped": True}
    async with _lock:
        if not await asyncio.to_thread(_ai_enabled):
            return {**empty, "error": "AI is disabled — enable it in Settings first"}
        if not config.anthropic_api_key():
            return {**empty, "error": "ANTHROPIC_API_KEY is not set"}

        view = await asyncio.to_thread(_plan, limit)
        jobs = view["candidates"]
        result = {**empty, "candidates": len(jobs), "waiting": view["waiting"],
                  "done_today": view["done_today"], "room": view["room"]}
        if not jobs:
            return result

        spent_before = await asyncio.to_thread(_cost_usd)
        for index, job in enumerate(jobs):
            if index:
                await asyncio.sleep(BATCH_PAUSE_S)
            try:
                drafted = await drafting.draft_for_job(job["id"])
            except Exception as exc:  # noqa: BLE001 - one posting, not the pass
                log.warning("preparing: job %s raised: %s", job["id"], exc)
                result["failed"] += 1
                result["errors"].append(f"{job['company']}: {exc}")
                continue
            if not drafted["ok"]:
                result["failed"] += 1
                result["errors"].append(f"{job['company']}: {drafted['error']}")
                continue
            result["prepared"] += 1
            try:
                built = await mappe.create_mappe(job["id"])
            except Exception as exc:  # noqa: BLE001 - the letter is written
                log.warning("preparing: Mappe for %s raised: %s", job["id"], exc)
                built = {"ok": False, "error": str(exc)}
            if not built["ok"]:
                # the draft stands; only the PDF is missing, and the queue says so
                result["no_mappe"] += 1
                result["errors"].append(
                    f"{job['company']}: Mappe — {built['error']}")
        spent_after = await asyncio.to_thread(_cost_usd)
        result["cost_usd"] = round(max(0.0, spent_after - spent_before), 4)
        log.info("preparing: %s prepared, %s failed, %s without a Mappe, $%.4f",
                 result["prepared"], result["failed"], result["no_mappe"],
                 result["cost_usd"])
        return result


def _ai_enabled() -> bool:
    with db.db() as con:
        return db.ai_enabled(con)


def _cost_usd() -> float:
    with db.db() as con:
        try:
            return float(db.get_setting(con, "llm_cost_usd", "0") or 0)
        except ValueError:
            return 0.0
