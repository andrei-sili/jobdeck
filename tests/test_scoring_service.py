import pytest

from jobdeck import config, db
from jobdeck.ai import llm
from jobdeck.services import scoring


def _insert_job(con, external_id="j1", **over):
    values = dict(
        source="stub", external_id=external_id, title="Python Dev",
        company="Firma", description="desc",
    )
    values.update(over)
    return db.insert_job_if_new(con, values)


def _usage(cost=0.001):
    return llm.LLMResult(
        text="", model="claude-haiku-4-5",
        input_tokens=10, output_tokens=5, cost_usd=cost,
    )


@pytest.fixture(autouse=True)
def _fresh_attempt_counters(monkeypatch):
    """The retry-cap dict is module state — isolate it per test."""
    monkeypatch.setattr(scoring, "_attempts", {})


@pytest.fixture()
def profile_file(data_dir):
    config.PROFILE_PATH.write_text("Python developer, 3 years", encoding="utf-8")
    return config.PROFILE_PATH


@pytest.fixture()
def ai_on(con):
    """The master AI toggle defaults to off — scoring tests opt in."""
    db.set_setting(con, "ai_enabled", "1")
    con.commit()


async def test_scores_unscored_new_jobs(con, profile_file, ai_on, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _insert_job(con, "j1")
    _insert_job(con, "j2")
    _insert_job(con, "j3", status="portal")  # not 'new' — must stay unscored
    con.commit()

    monkeypatch.setattr(
        "jobdeck.ai.scoring.score_job",
        lambda job, profile_text, criteria=None: (66, "Guter Fit.", {}, _usage()),
    )

    counters = await scoring.score_new_jobs()
    assert counters == {"scored": 2, "failed": 0}

    jobs = {j["external_id"]: j for j in db.list_jobs(con)}
    assert jobs["j1"]["match_score"] == 66
    assert jobs["j1"]["match_reason"] == "Guter Fit."
    assert jobs["j2"]["match_score"] == 66
    assert jobs["j3"]["match_score"] is None

    # metering accumulated in app_settings
    assert db.get_setting(con, "llm_calls") == "2"
    assert db.get_setting(con, "llm_input_tokens") == "20"
    assert db.get_setting(con, "llm_output_tokens") == "10"
    assert float(db.get_setting(con, "llm_cost_usd")) == pytest.approx(0.002)


async def test_second_run_has_nothing_left_to_score(con, profile_file, ai_on, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _insert_job(con, "j1")
    con.commit()
    monkeypatch.setattr(
        "jobdeck.ai.scoring.score_job",
        lambda job, profile_text, criteria=None: (50, "Ok.", {}, _usage()),
    )

    assert (await scoring.score_new_jobs())["scored"] == 1
    assert (await scoring.score_new_jobs())["scored"] == 0


async def test_skips_when_ai_disabled(con, profile_file, monkeypatch):
    """The master toggle (default off) blocks every LLM call, key or no key."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _insert_job(con)
    con.commit()

    monkeypatch.setattr("jobdeck.ai.scoring.score_job", _must_not_be_called)

    counters = await scoring.score_new_jobs()
    assert counters == {"scored": 0, "failed": 0}
    assert db.list_jobs(con)[0]["match_score"] is None
    assert db.get_setting(con, "llm_calls", "0") == "0"


def _must_not_be_called(job, profile_text, criteria=None):
    raise AssertionError("LLM called although a skip gate should have fired")


async def test_skips_without_api_key(con, profile_file, ai_on, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("jobdeck.ai.scoring.score_job", _must_not_be_called)
    _insert_job(con)
    con.commit()

    counters = await scoring.score_new_jobs()
    assert counters == {"scored": 0, "failed": 0}
    assert db.list_jobs(con)[0]["match_score"] is None


async def test_skips_without_profile(con, ai_on, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("jobdeck.ai.scoring.score_job", _must_not_be_called)
    _insert_job(con)
    con.commit()

    counters = await scoring.score_new_jobs()
    assert counters == {"scored": 0, "failed": 0}
    assert db.list_jobs(con)[0]["match_score"] is None


async def test_one_failure_does_not_block_the_rest(con, profile_file, ai_on, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _insert_job(con, "bad")
    _insert_job(con, "good")
    con.commit()

    def fake_score(job, profile_text, criteria=None):
        if job["external_id"] == "bad":
            raise llm.LLMError("boom")
        return (80, "Passt.", {}, _usage())

    monkeypatch.setattr("jobdeck.ai.scoring.score_job", fake_score)

    counters = await scoring.score_new_jobs()
    assert counters == {"scored": 1, "failed": 1}
    jobs = {j["external_id"]: j for j in db.list_jobs(con)}
    assert jobs["good"]["match_score"] == 80
    assert jobs["bad"]["match_score"] is None


async def test_batch_limit_caps_llm_calls_per_run(con, profile_file, ai_on, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    for i in range(3):
        _insert_job(con, f"j{i}")
    con.commit()
    monkeypatch.setattr(
        "jobdeck.ai.scoring.score_job",
        lambda job, profile_text, criteria=None: (50, "Ok.", {}, _usage()),
    )

    assert (await scoring.score_new_jobs(limit=2))["scored"] == 2
    assert db.get_setting(con, "llm_calls") == "2"
    assert (await scoring.score_new_jobs(limit=2))["scored"] == 1


async def test_concurrent_runs_never_double_score(con, profile_file, ai_on, monkeypatch):
    import asyncio
    import time

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    for i in range(4):
        _insert_job(con, f"j{i}")
    con.commit()

    calls = []

    def slow_score(job, profile_text, criteria=None):
        calls.append(job["id"])
        time.sleep(0.02)  # widen the overlap window
        return (60, "Ok.", {}, _usage())

    monkeypatch.setattr("jobdeck.ai.scoring.score_job", slow_score)

    # scheduler run and manual settings-page click at the same time
    results = await asyncio.gather(scoring.score_new_jobs(), scoring.score_new_jobs())
    assert sum(r["scored"] for r in results) == 4
    assert len(calls) == 4  # every job billed exactly once
    assert db.get_setting(con, "llm_calls") == "4"


async def test_failed_calls_are_still_metered(con, profile_file, ai_on, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _insert_job(con, "bad")
    con.commit()

    def fake_score(job, profile_text, criteria=None):
        raise llm.LLMError("unparseable", usage=_usage(cost=0.003))

    monkeypatch.setattr("jobdeck.ai.scoring.score_job", fake_score)

    counters = await scoring.score_new_jobs()
    assert counters == {"scored": 0, "failed": 1}
    assert db.list_jobs(con)[0]["match_score"] is None
    assert db.get_setting(con, "llm_calls") == "1"
    assert float(db.get_setting(con, "llm_cost_usd")) == pytest.approx(0.003)


async def test_extracted_contacts_are_persisted(con, profile_file, ai_on, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _insert_job(con, "j1")
    con.commit()

    monkeypatch.setattr(
        "jobdeck.ai.scoring.score_job",
        lambda job, profile_text, criteria=None: (
            70, "Passt gut.",
            {"ansprechpartner": "Frau Weber", "contact_email": "jobs@firma.de",
             "refnr": "K-2026-17"},
            _usage(),
        ),
    )

    assert (await scoring.score_new_jobs())["scored"] == 1
    job = db.list_jobs(con)[0]
    assert job["match_score"] == 70
    assert job["ansprechpartner"] == "Frau Weber"
    assert job["contact_email"] == "jobs@firma.de"
    assert job["refnr"] == "K-2026-17"
    assert job["contact_source"] == "posting"


async def test_kill_switch_stops_an_in_flight_batch(
    con, profile_file, ai_on, monkeypatch
):
    """Flipping AI off mid-batch must stop before the next paid call."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    for i in range(3):
        _insert_job(con, f"j{i}")
    con.commit()

    calls = []

    def flip_off_after_first(job, profile_text, criteria=None):
        calls.append(job["id"])
        with db.db() as c:  # the Settings switch writes from another thread
            db.set_setting(c, "ai_enabled", "0")
        return (60, "Ok.", {}, _usage())

    monkeypatch.setattr("jobdeck.ai.scoring.score_job", flip_off_after_first)

    counters = await scoring.score_new_jobs()
    assert counters == {"scored": 1, "failed": 0}
    assert len(calls) == 1  # jobs 2 and 3 were never sent to the API
    assert db.get_setting(con, "llm_calls") == "1"


async def test_profile_criteria_reach_the_scoring_call(
    con, profile_file, ai_on, monkeypatch
):
    from jobdeck.ai import scoring as ai_scoring

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    strict = db.add_profile(
        con, {"name": "Strict", "keywords": "Python",
              "hard_tags": "#backend, #remote", "strictness": 80},
    )
    plain = db.add_profile(con, {"name": "Plain", "keywords": "Python"})
    _insert_job(con, "with-criteria", profile_id=strict)
    _insert_job(con, "plain-profile", profile_id=plain)
    _insert_job(con, "orphan")  # discovered profile is gone → profile_id None
    con.commit()

    received = {}

    def fake_score(job, profile_text, criteria=None):
        received[job["external_id"]] = criteria
        return (60, "Ok.", {}, _usage())

    monkeypatch.setattr("jobdeck.ai.scoring.score_job", fake_score)

    assert (await scoring.score_new_jobs())["scored"] == 3
    assert received["with-criteria"] == ai_scoring.MatchCriteria(
        hard_tags=("#backend", "#remote"), strictness=80
    )
    assert received["plain-profile"] is None  # defaults → prompt unchanged
    assert received["orphan"] is None


async def test_retry_cap_gives_up_and_stops_starving_the_batch(
    con, profile_file, ai_on, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _insert_job(con, "always-bad")  # oldest — would head every batch forever
    con.commit()

    attempts = []

    def fake_score(job, profile_text, criteria=None):
        if job["external_id"] == "always-bad":
            attempts.append(job["id"])
            raise llm.LLMError("boom")
        return (70, "Ok.", {}, _usage())

    monkeypatch.setattr("jobdeck.ai.scoring.score_job", fake_score)

    for _ in range(scoring.MAX_ATTEMPTS + 2):
        await scoring.score_new_jobs(limit=1)
    assert len(attempts) == scoring.MAX_ATTEMPTS  # no endless paid retries

    # the given-up job no longer occupies the batch slot
    _insert_job(con, "fresh")
    con.commit()
    assert (await scoring.score_new_jobs(limit=1))["scored"] == 1
    jobs = {j["external_id"]: j for j in db.list_jobs(con)}
    assert jobs["fresh"]["match_score"] == 70
    assert jobs["always-bad"]["match_score"] is None


def test_list_unscored_jobs_orders_limits_and_excludes(con):
    ids = [_insert_job(con, f"j{i}") for i in range(3)]
    con.commit()

    rows = db.list_unscored_jobs(con, limit=2)
    assert [r["id"] for r in rows] == ids[:2]  # oldest first, capped

    rows = db.list_unscored_jobs(con, limit=2, exclude_ids={ids[0]})
    assert [r["id"] for r in rows] == ids[1:3]
