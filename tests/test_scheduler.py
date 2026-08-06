"""The background jobs the app really registers."""

import datetime

import pytest

from jobdeck import scheduler


@pytest.fixture(autouse=True)
def _fresh_scheduler():
    scheduler.shutdown_scheduler()
    yield
    scheduler.shutdown_scheduler()


def test_every_background_job_is_registered_once_and_single_flight():
    jobs = {job.id: job for job in scheduler.create_scheduler().get_jobs()}
    assert sorted(jobs) == ["auto_send", "check_liveness", "poll_profiles",
                            "score_jobs"]
    for job in jobs.values():
        # a slow run must never stack up behind itself
        assert job.coalesce is True
        assert job.max_instances == 1


def test_create_scheduler_returns_the_same_instance():
    # two schedulers would double-run every job — the app must not be able to
    assert scheduler.create_scheduler() is scheduler.create_scheduler()


def test_the_liveness_pass_starts_without_waiting_a_full_interval():
    """An interval job first fires one interval in; his sessions are shorter
    than six hours, so the pass would never run at all."""
    job = {j.id: j for j in scheduler.create_scheduler().get_jobs()}["check_liveness"]
    delay = job.next_run_time - datetime.datetime.now(job.next_run_time.tzinfo)
    assert datetime.timedelta(0) < delay < datetime.timedelta(minutes=5)
    assert job.trigger.interval == datetime.timedelta(hours=6)
