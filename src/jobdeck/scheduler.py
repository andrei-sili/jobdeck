"""Background job scheduling on the shared asyncio event loop.

One scheduler instance per process; jobs are coalesced and single-flight
so a slow run never stacks up behind itself.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from jobdeck.services import autosend, polling, scoring

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def create_scheduler() -> AsyncIOScheduler:
    """Build (once) and return the application scheduler."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    scheduler = AsyncIOScheduler(timezone="Europe/Berlin")
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
        autosend.tick,
        "interval",
        minutes=1,  # cheap due-check; business hours + spacing gate real work
        id="auto_send",
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
