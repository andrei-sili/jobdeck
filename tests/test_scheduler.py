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
                            "resolve_channels", "score_jobs"]
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


def test_the_channel_backlog_starts_draining_inside_this_session():
    """Same reason as the liveness pass, and it is the whole feature: 738 of his
    908 postings have no apply channel, so on four rows out of five the screen
    could only offer "Kanal ermitteln" instead of a way to apply.

    60 a pass at half-hourly drains that in about six hours of uptime; at the
    liveness pass's six-hourly it would take seventy."""
    job = {j.id: j for j in scheduler.create_scheduler().get_jobs()}["resolve_channels"]
    delay = job.next_run_time - datetime.datetime.now(job.next_run_time.tzinfo)
    assert datetime.timedelta(0) < delay < datetime.timedelta(minutes=5)
    assert job.trigger.interval == datetime.timedelta(minutes=30)


def test_the_two_startup_passes_do_not_land_together():
    """Both fire shortly after launch and both make outbound requests. Landing
    on the same second doubles what other people's servers see from one app
    starting up."""
    jobs = {j.id: j for j in scheduler.create_scheduler().get_jobs()}
    apart = abs(jobs["resolve_channels"].next_run_time
                - jobs["check_liveness"].next_run_time)
    assert apart >= datetime.timedelta(seconds=20)
