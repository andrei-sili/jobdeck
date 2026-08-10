"""The daily prepare batch: which postings it picks, and what it refuses to do.

Every draft is a paid model call, so the selection rules are money rules. The
tests that matter here are the ones that stop a wasted or duplicated one.
"""

import datetime

import pytest

from jobdeck import db
from jobdeck.services import preparing


def _job(con, ext, *, company="Firma", score=80, age_days=3, email="",
         status="new", liveness="alive", duplicate_of=None):
    job_id = db.insert_job_if_new(con, {
        "source": "arbeitsagentur", "external_id": ext, "title": "Python Dev",
        "company": company, "url": f"https://firma.de/{ext}",
        "contact_email": email, "status": status,
    })
    published = (datetime.date.today() - datetime.timedelta(days=age_days)).isoformat()
    con.execute(
        "UPDATE jobs SET match_score=?, published_on=?, liveness=?, duplicate_of=? "
        "WHERE id=?", (score, published, liveness, duplicate_of, job_id))
    return job_id


def _draft(con, job_id, status="ready"):
    db.upsert_draft(con, job_id, {
        "status": status, "recipient": "hr@firma.de", "betreff": "B",
        "email_body": "e", "anschreiben_body": "a", "pdf_path": "/tmp/m.pdf"})


def _picked(con, **over):
    cfg = {"limit": 10, "max_age_days": 21, "min_score": 70, "include_forms": True}
    cfg.update(over)
    return [r["external_id"] for r in db.jobs_to_prepare(con, **cfg)]


def test_the_filter_he_asked_for_is_the_filter_that_runs(con, data_dir):
    _job(con, "fresh-good", score=82, age_days=3)
    _job(con, "too-old", score=95, age_days=40)          # great fit, stale ad
    _job(con, "too-weak", score=60, age_days=1)          # fresh, poor fit
    _job(con, "edge-age", score=70, age_days=21)         # exactly on both bounds
    con.commit()
    # both bounds are inclusive, and the order is by AGED score — so the fresh 82
    # leads the 70 sitting exactly on the age limit
    assert _picked(con) == ["fresh-good", "edge-age"]


def test_an_unreadable_date_falls_out_of_the_age_bound(con, data_dir):
    """The inbox must not hide a posting for missing information, but a paid
    letter should go to one we can vouch is current — and that follows from the
    age bound itself: an unknown date makes the age NULL, and NULL <= 21 is
    never true. No separate clause, so none can rot."""
    job_id = _job(con, "dateless", score=90)
    con.execute("UPDATE jobs SET published_on='' WHERE id=?", (job_id,))
    con.commit()
    assert _picked(con) == []


@pytest.mark.parametrize("liveness, status, picked", [
    ("alive", "new", True),
    ("gone", "new", False),        # the ad is down — the letter would be waste
    ("alive", "portal", False),    # already being worked on
    ("alive", "applied", False),
    ("alive", "skipped", False),
])
def test_only_a_live_untouched_posting_is_a_candidate(con, data_dir, liveness,
                                                      status, picked):
    _job(con, "x", liveness=liveness, status=status)
    con.commit()
    assert (_picked(con) == ["x"]) is picked


def test_a_company_already_applied_to_is_never_drafted_again(con, data_dir):
    """find_duplicate_bewerbung would refuse the application anyway, so paying
    for the letter is pure waste — and the comparison must use the SAME
    normalisation as that gate, not SQLite's ASCII-only lower()."""
    db.add_bewerbung(con, {"gesendet_am": "2026-08-01", "firma": "MÜLLER GmbH",
                           "email": "", "kanal": "E-Mail", "status": "Gesendet"})
    _job(con, "same", company="müller gmbh ")
    _job(con, "other", company="Andere AG")
    con.commit()
    assert _picked(con) == ["other"]


@pytest.mark.parametrize("draft_status, picked", [
    ("ready", False), ("approved", False), ("sent", False),
    ("sending", False), ("generating", False),
    ("discarded", True),   # he threw it away: the posting is a candidate again
    ("failed", True),      # drafting broke: worth another attempt
])
def test_a_posting_already_in_flight_is_not_drafted_twice(con, data_dir,
                                                          draft_status, picked):
    job_id = _job(con, "x")
    _draft(con, job_id, draft_status)
    con.commit()
    assert (_picked(con) == ["x"]) is picked


def test_forms_can_be_excluded_but_then_almost_nothing_qualifies(con, data_dir):
    _job(con, "with-mail", email="jobs@firma.de", company="A GmbH")
    _job(con, "form-only", company="B GmbH")
    con.commit()
    assert set(_picked(con)) == {"with-mail", "form-only"}
    assert _picked(con, include_forms=False) == ["with-mail"]


def test_the_quota_counts_what_is_WAITING_not_what_was_created(con, data_dir):
    """He asked to SEE five applications in the queue. A draft he discarded must
    free its place; one he sent must free it too."""
    for n in range(3):
        job_id = _job(con, f"w{n}", company=f"F{n}")
        _draft(con, job_id, "ready")
    gone = _job(con, "thrown-away", company="Weg GmbH")
    _draft(con, gone, "discarded")
    sent = _job(con, "already-sent", company="Sent GmbH", status="applied")
    _draft(con, sent, "sent")
    con.commit()
    assert db.count_waiting_drafts(con) == 3        # not 5
    assert db.count_drafts_created_today(con) == 5  # reported, never the quota

    # and the PLAN must use the waiting count: with per_day 5 there is room for
    # 2 more. Measured against drafts CREATED today it would be 0 and he could
    # never top the queue back up after discarding.
    db.set_setting(con, "prepare_per_day", "5")
    _job(con, "candidate", company="Neu GmbH", score=90, age_days=1)
    con.commit()
    view = preparing.plan()
    assert view["waiting"] == 3 and view["done_today"] == 5
    assert view["room"] == 2
    # the posting whose draft he THREW AWAY is deliberately a candidate again,
    # so the two open places go to it and the fresh one, best score first
    assert [j["external_id"] for j in view["candidates"]] == ["candidate",
                                                             "thrown-away"]


def test_the_plan_spends_nothing_and_says_what_it_would_cost(con, data_dir,
                                                            monkeypatch):
    for n in range(2):
        _job(con, f"c{n}", company=f"F{n}", score=85, age_days=n + 1)
    con.commit()
    calls = []
    monkeypatch.setattr(preparing.drafting, "draft_for_job",
                        lambda job_id: calls.append(job_id))
    view = preparing.plan()
    assert calls == []                                     # nothing was written
    assert len(view["candidates"]) == 2
    assert view["estimate_usd"] == round(2 * preparing.COST_PER_DRAFT_USD, 2)
    assert view["room"] == view["config"]["per_day"]        # queue is empty


async def test_a_run_stops_at_the_daily_number(con, data_dir, monkeypatch):
    for n in range(9):
        _job(con, f"c{n}", company=f"F{n}", score=90 - n, age_days=1)
    db.set_setting(con, "prepare_per_day", "3")
    db.set_setting(con, "ai_enabled", "1")
    con.commit()
    monkeypatch.setattr(preparing, "BATCH_PAUSE_S", 0)
    monkeypatch.setattr(preparing.config, "anthropic_api_key", lambda: "k")
    drafted, built = [], []

    async def fake_draft(job_id):
        drafted.append(job_id)
        with db.db() as c:
            _draft(c, job_id, "ready")
        return {"ok": True, "error": "", "draft": {}}

    async def fake_mappe(job_id):
        built.append(job_id)
        return {"ok": True, "error": "", "pdf_path": "/tmp/m.pdf"}

    monkeypatch.setattr(preparing.drafting, "draft_for_job", fake_draft)
    monkeypatch.setattr(preparing.mappe, "create_mappe", fake_mappe)

    res = await preparing.prepare_day()
    assert res["prepared"] == 3 and len(drafted) == 3
    assert built == drafted            # every letter also gets its Mappe
    # best-scored first
    scores = [con.execute("SELECT match_score FROM jobs WHERE id=?", (j,)
                          ).fetchone()[0] for j in drafted]
    assert scores == sorted(scores, reverse=True)

    # pressing again prepares nothing: the queue is already at the number
    res2 = await preparing.prepare_day()
    assert res2["prepared"] == 0 and res2["candidates"] == 0
    assert len(drafted) == 3


async def test_one_failing_posting_does_not_end_the_run(con, data_dir, monkeypatch):
    for n in range(3):
        _job(con, f"c{n}", company=f"F{n}", score=90 - n, age_days=1)
    db.set_setting(con, "ai_enabled", "1")
    con.commit()
    monkeypatch.setattr(preparing, "BATCH_PAUSE_S", 0)
    monkeypatch.setattr(preparing.config, "anthropic_api_key", lambda: "k")
    seen = []

    async def flaky_draft(job_id):
        seen.append(job_id)
        if len(seen) == 1:
            return {"ok": False, "error": "the model looped", "draft": None}
        with db.db() as c:
            _draft(c, job_id, "ready")
        return {"ok": True, "error": "", "draft": {}}

    async def broken_mappe(job_id):
        raise RuntimeError("the template is missing")

    monkeypatch.setattr(preparing.drafting, "draft_for_job", flaky_draft)
    monkeypatch.setattr(preparing.mappe, "create_mappe", broken_mappe)

    res = await preparing.prepare_day()
    assert len(seen) == 3                  # the failure did not end the pass
    assert res["failed"] == 1 and res["prepared"] == 2
    # a Mappe that cannot be built leaves the LETTER standing, and says so
    assert res["no_mappe"] == 2
    assert any("the model looped" in e for e in res["errors"])


async def test_two_runs_never_overlap(con, data_dir, monkeypatch):
    import asyncio
    _job(con, "c0", score=90, age_days=1)
    db.set_setting(con, "ai_enabled", "1")
    con.commit()
    monkeypatch.setattr(preparing, "BATCH_PAUSE_S", 0)
    monkeypatch.setattr(preparing.config, "anthropic_api_key", lambda: "k")
    started = []

    async def slow_draft(job_id):
        started.append(job_id)
        await asyncio.sleep(0.05)
        with db.db() as c:
            _draft(c, job_id, "ready")
        return {"ok": True, "error": "", "draft": {}}

    monkeypatch.setattr(preparing.drafting, "draft_for_job", slow_draft)
    monkeypatch.setattr(preparing.mappe, "create_mappe",
                        lambda job_id: _ok_mappe())

    async def _ok_mappe():
        return {"ok": True, "error": "", "pdf_path": "/tmp/m.pdf"}

    first, second = await asyncio.gather(preparing.prepare_day(),
                                         preparing.prepare_day())
    ran, skipped = sorted((first, second), key=lambda r: -r["prepared"])
    assert ran["prepared"] == 1
    assert skipped.get("skipped") is True      # not queued behind it
    assert len(started) == 1                   # nobody paid twice


async def test_nothing_is_written_while_ai_is_off(con, data_dir, monkeypatch):
    _job(con, "c0", score=90, age_days=1)
    db.set_setting(con, "ai_enabled", "0")
    con.commit()
    called = []
    monkeypatch.setattr(preparing.drafting, "draft_for_job",
                        lambda job_id: called.append(job_id))
    res = await preparing.prepare_day()
    assert called == [] and res["prepared"] == 0
    assert "disabled" in res["error"]


def test_the_defaults_are_the_numbers_he_chose():
    """Pinned as literals: "the run honours the settings" stays green if someone
    changes what the settings default to."""
    assert preparing.DEFAULT_MAX_AGE_DAYS == 21
    assert preparing.DEFAULT_MIN_SCORE == 70
    assert preparing.DEFAULT_PER_DAY == 5
    assert preparing.DEFAULT_INCLUDE_FORMS is True


def test_a_hand_edited_filter_setting_cannot_break_the_page(con, data_dir):
    for value in ("", "abc", "-5", "3.7", "1e999"):
        db.set_setting(con, "prepare_min_score", value)
        cfg = preparing.settings(con)
        assert isinstance(cfg["min_score"], int) and cfg["min_score"] >= 0
