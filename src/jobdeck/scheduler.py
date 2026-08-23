"""Background job scheduling on the shared asyncio event loop.

One scheduler instance per process; jobs are coalesced and single-flight
so a slow run never stacks up behind itself.
"""

import datetime
import logging
import zoneinfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from jobdeck.services import (
    apply_resolve,
    autosend,
    liveness,
    polling,
    replies,
    scoring,
    silence,
)

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

# Auto-send's business hours are Europe/Berlin, so the whole scheduler runs in
# it; every datetime handed to APScheduler is built in that zone rather than
# left naive, which would silently mean "the machine's zone" instead.
TIMEZONE = zoneinfo.ZoneInfo("Europe/Berlin")


def create_scheduler() -> AsyncIOScheduler:
    """Build (once) and return the application scheduler."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        polling.poll_all_profiles,
        "interval",
        minutes=5,  # cheap due-check; per-profile intervals decide real work
        id="poll_profiles",
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        scoring.score_new_jobs,
        "interval",
        minutes=10,  # no-op while unconfigured or when nothing is unscored
        id="score_jobs",
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        liveness.check_pending,
        "interval",
        hours=6,
        # An interval job first fires one interval in, and normal local runs
        # are shorter than six hours — the pass would never run. It starts
        # shortly after launch instead, and the per-posting recheck window
        # (RECHECK_AFTER_H) is what keeps repeated restarts from re-probing
        # anything: the batch is bounded by that, not by the tick.
        next_run_time=(datetime.datetime.now(TIMEZONE)
                       + datetime.timedelta(seconds=90)),
        id="check_liveness",
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        apply_resolve.resolve_pending,
        "interval",
        # 60 postings a pass, so half-hourly drains a backlog of ~740 in about
        # six hours of uptime; six-hourly would need seventy-odd. This is what
        # deletes "Kanal ermitteln" as a button: the answer is simply there by
        # the time he reads the posting, on four rows out of five.
        minutes=30,
        # Same reason as the liveness pass: an interval job first fires one
        # interval in, and the first pass has to happen during a normal run. 120 s
        # so it does not land on top of that pass at 90 s.
        next_run_time=(datetime.datetime.now(TIMEZONE)
                       + datetime.timedelta(seconds=120)),
        id="resolve_channels",
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        autosend.tick,
        "interval",
        minutes=1,  # cheap due-check; business hours + spacing gate real work
        id="auto_send",
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        replies.ingest_replies,
        "interval",
        minutes=10,  # current reply-polling cadence; see Current Delivery State
        # First pass shortly after launch, like liveness and the channel
        # resolver, and staggered behind both (90 s / 120 s): a reply he is
        # waiting on should not wait for the first full interval — and with
        # no read permission yet, the pass costs one file read and writes
        # the honest banner instead.
        next_run_time=(datetime.datetime.now(TIMEZONE)
                       + datetime.timedelta(seconds=150)),
        id="ingest_replies",
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        silence.close_silent,
        "interval",
        hours=12,
        # Silence is measured in days, so the cadence only has to be well
        # under one — twice a day means a row closes on the day it is due
        # whether or not he happens to run the app in the morning. It runs
        # AFTER the reply pass has had its first go (150 s): a reply sitting
        # unread in the mailbox must get its chance to restart the clock
        # before anything is closed for not having arrived.
        next_run_time=(datetime.datetime.now(TIMEZONE)
                       + datetime.timedelta(seconds=210)),
        id="close_silent",
        coalesce=True,
        max_instances=1,
    )
    _scheduler = scheduler
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
