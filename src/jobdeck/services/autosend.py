"""Automatic transmission of human-approved drafts, paced for reputation.

Auto-send never composes or approves anything: it only transmits drafts
the user explicitly approved, one per tick at most, for jobs whose search
profile opted in (auto_send, default OFF). Pacing is a deliverability
measure (researched 2026-07): sends happen in business hours
(Mon–Fri, Europe/Berlin) with randomized spacing, and the shared daily
cap applies. Any failure returns the draft to 'ready' with the reason
recorded — a draft never loops through failing sends unattended.
"""

import asyncio
import datetime
import logging
import random
from zoneinfo import ZoneInfo

from jobdeck import db, gmail
from jobdeck.services import send

log = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Berlin")
BUSINESS_START_H = 9
BUSINESS_END_H = 17
SPACING_MIN_MIN = 8.0
SPACING_MAX_MIN = 22.0
NEXT_SEND_KEY = "next_auto_send_at"


def _now_berlin() -> datetime.datetime:
    return datetime.datetime.now(TZ)


def _in_business_hours(now: datetime.datetime) -> bool:
    return now.weekday() < 5 and BUSINESS_START_H <= now.hour < BUSINESS_END_H


def _due(con, now: datetime.datetime) -> bool:
    raw = db.get_setting(con, NEXT_SEND_KEY, "")
    if not raw:
        return True
    try:
        next_at = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return True  # unreadable state must not wedge auto-send forever
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=TZ)
    return now >= next_at


def _global_block(con) -> str:
    """Conditions that pause the whole queue (no draft is at fault)."""
    if (db.get_setting(con, "real_send_enabled", "0") != "1"
            and not db.get_setting(con, "test_recipient", "").strip()):
        return "test mode without a test recipient"
    cap = int(db.get_setting(con, "daily_send_cap", "15") or "15")
    if db.count_outbound_today(con) >= cap:
        return "daily cap reached"
    return ""


def _pick(now: datetime.datetime):
    with db.db() as con:
        block = _global_block(con)
        if block:
            return block, None
        if not _due(con, now):
            return "waiting for the next send window", None
        # In test mode a send leaves the draft approved, so each draft is
        # rehearsed once and the queue still drains — otherwise the worker
        # would re-pick the same posting until the daily cap ran out.
        test_mode = db.get_setting(con, "real_send_enabled", "0") != "1"
        job_id = db.next_approved_autosend_job(con, exclude_test_sent=test_mode)
        if (job_id is None and test_mode
                and db.next_approved_autosend_job(con) is not None):
            return ("every approved draft was already rehearsed to the test "
                    "inbox — enable real sending to send them"), None
        return "", job_id


def _schedule_next(now: datetime.datetime) -> str:
    spacing_min = random.uniform(SPACING_MIN_MIN, SPACING_MAX_MIN)
    next_at = (now + datetime.timedelta(minutes=spacing_min)).isoformat(
        timespec="seconds"
    )
    with db.db() as con:
        db.set_setting(con, NEXT_SEND_KEY, next_at)
    return next_at


async def tick() -> dict:
    """One scheduler tick: transmit at most one approved draft.

    Returns {"sent": 0|1, "reason": str, ...} for logging and tests."""
    now = _now_berlin()
    if not _in_business_hours(now):
        return {"sent": 0, "reason": "outside business hours"}
    if not gmail.is_connected():
        return {"sent": 0, "reason": "gmail not connected"}
    block, job_id = await asyncio.to_thread(_pick, now)
    if block:
        return {"sent": 0, "reason": block}
    if job_id is None:
        return {"sent": 0, "reason": "nothing approved for auto-send"}

    # The pick and the send are not one transaction (send_draft waits for the
    # send lock): pin the status so an un-approval landing in that window
    # cannot be overridden.
    try:
        result = await send.send_draft(job_id, expect={"status": "approved"})
    except Exception as exc:
        # send_draft released the claim and re-raised. Demote here too:
        # without it the draft stays 'approved' and every tick retries it.
        await asyncio.to_thread(
            send.demote_failed_autosend, job_id, f"unexpected error: {exc}"
        )
        log.exception("auto-send for job %s raised", job_id)
        return {"sent": 0, "reason": f"unexpected error: {exc}",
                "job_id": job_id}
    if not result["ok"]:
        if result.get("kind") == "global":
            # Nothing is wrong with this draft — pause instead of draining
            # the approved pool one innocent draft per tick.
            log.info("auto-send paused: %s", result["error"])
            return {"sent": 0, "reason": result["error"]}
        # Fail toward human attention, never toward unattended retries.
        await asyncio.to_thread(
            send.demote_failed_autosend, job_id, result["error"]
        )
        log.warning("auto-send for job %s failed: %s", job_id, result["error"])
        return {"sent": 0, "reason": result["error"], "job_id": job_id}

    next_at = await asyncio.to_thread(_schedule_next, now)
    log.info("auto-sent job %s to %s (next window from %s)",
             job_id, result["recipient"], next_at)
    return {"sent": 1, "reason": "", "job_id": job_id,
            "recipient": result["recipient"], "test_mode": result["test_mode"]}
