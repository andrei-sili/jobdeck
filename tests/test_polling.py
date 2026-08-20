import asyncio
import json

import pytest

from jobdeck import db
from jobdeck.services import polling
from jobdeck.sources.base import JobPosting, SearchQuery, SourceUnavailable


class StubSource:
    def __init__(self, name, postings=None, error=None):
        self.name = name
        self._postings = postings or []
        self._error = error

    async def search(self, query: SearchQuery):
        if self._error:
            raise SourceUnavailable(self.name, self._error)
        return list(self._postings)

    async def fetch_details(self, posting):
        return posting


def _posting(source="stub", external_id="j1", company="Firma A", **over):
    values = dict(
        source=source, external_id=external_id, title="Python Dev",
        company=company, url="https://x/1", description="desc",
        contact_email="hr@firma-a.de",
    )
    values.update(over)
    return JobPosting(**values)


@pytest.fixture()
def profile(con):
    db.add_profile(
        con,
        {"name": "Test", "keywords": "python", "sources": ["stub", "broken"]},
    )
    con.commit()
    return db.list_profiles(con)[0]


async def test_poll_profile_stores_new_jobs(con, profile, monkeypatch):
    stub = StubSource("stub", [_posting(), _posting(external_id="j2", company="Firma B")])
    broken = StubSource("broken", error="boom")
    monkeypatch.setattr(polling, "get_sources",
                        lambda client: {"stub": stub, "broken": broken})

    counters = await polling.poll_profile(profile)
    assert counters["new"] == 2

    jobs = db.list_jobs(con)
    assert len(jobs) == 2
    # degraded source recorded on the profile, polling still succeeded
    prof = db.list_profiles(con)[0]
    assert "boom" in (prof["last_poll_error"] or "")
    assert prof["last_polled_at"]


async def test_poll_profile_marks_already_applied_companies(con, profile, monkeypatch):
    db.add_bewerbung(con, {"firma": "Firma A", "status": "Gesendet"})
    con.commit()
    stub = StubSource("stub", [_posting()])
    monkeypatch.setattr(polling, "get_sources", lambda client: {"stub": stub})

    counters = await polling.poll_profile(profile)
    assert counters["duplicate"] == 1
    job = db.list_jobs(con)[0]
    assert job["status"] == "duplicate" and job["duplicate_of"] is not None


async def test_poll_profile_skips_cross_source_duplicates(con, profile, monkeypatch):
    first = StubSource("stub", [_posting()])
    monkeypatch.setattr(polling, "get_sources", lambda client: {"stub": first})
    await polling.poll_profile(profile)

    # same company+title arrives from another source id
    second = StubSource("stub", [_posting(external_id="other-id-999")])
    monkeypatch.setattr(polling, "get_sources", lambda client: {"stub": second})
    counters = await polling.poll_profile(profile)
    assert counters["known"] == 1
    assert len(db.list_jobs(con)) == 1


async def test_poll_all_profiles_respects_interval(con, profile, monkeypatch):
    calls = []

    async def fake_poll(p):
        calls.append(p["id"])
        return {"new": 0, "duplicate": 0, "known": 0}

    monkeypatch.setattr(polling, "poll_profile", fake_poll)
    await polling.poll_all_profiles()  # never polled -> due
    # simulate "just polled"
    with db.db() as c:
        db.mark_profile_polled(c, profile["id"])
    await polling.poll_all_profiles()  # not due anymore
    assert calls == [profile["id"]]

    await polling.poll_all_profiles(force=True)  # force ignores the interval
    assert calls == [profile["id"], profile["id"]]


async def test_profile_sources_json_roundtrip(con, profile):
    assert json.loads(profile["sources"]) == ["stub", "broken"]


async def test_a_stored_posting_keeps_the_facts_its_source_stated(
        con, profile, monkeypatch):
    """The Arbeitsagentur states a work address, a pay range and whether the
    job is Arbeitnehmerüberlassung; all of it used to be parsed and dropped."""
    posting = _posting(facts={"work_strasse": "Musterstraße 26",
                              "work_plz_ort": "54321 Beispielstadt",
                              "salary_from": "37000", "temp_agency": 1})
    monkeypatch.setattr(polling, "get_sources",
                        lambda client: {"stub": StubSource("stub", [posting])})

    await polling.poll_profile(profile)

    row = db.list_jobs(con)[0]
    assert row["work_strasse"] == "Musterstraße 26"
    assert row["work_plz_ort"] == "54321 Beispielstadt"
    assert row["salary_from"] == "37000" and row["temp_agency"] == 1


async def test_a_source_that_states_no_facts_stores_none(con, profile,
                                                          monkeypatch):
    monkeypatch.setattr(polling, "get_sources",
                        lambda client: {"stub": StubSource("stub", [_posting()])})

    await polling.poll_profile(profile)

    row = db.list_jobs(con)[0]
    assert row["work_strasse"] == "" and row["temp_agency"] == 0


# ---------------------------------------------------------------------------
# What the last search found. It was computed on every pass and thrown into a
# log line, so a search that found nothing looked exactly like one that never
# ran — which is half of what he was asking with "cind? cum?".
# ---------------------------------------------------------------------------
def _profile(con, name="Python", active=1):
    db.add_profile(con, {
        "name": name, "keywords": "python", "location": "", "radius_km": 0,
        "remote": 0, "sources": json.dumps(["stub"]), "active": active,
        "interval_minutes": 60,
    })
    con.commit()


async def test_a_pass_writes_down_what_it_found(con, data_dir, monkeypatch):
    _profile(con)

    async def one_new(profile):
        return {"new": 3, "duplicate": 1, "known": 12}

    monkeypatch.setattr(polling, "poll_profile", one_new)
    await polling.poll_all_profiles(force=True)

    with db.db() as fresh:
        report = polling.last_poll(fresh)
    assert (report["new"], report["duplicate"], report["known"]) == (3, 1, 12)
    assert report["profiles"] == 1
    assert report["by"] == "user"
    assert report["at"]


async def test_the_scheduled_pass_writes_the_same_receipt(con, data_dir,
                                                          monkeypatch):
    """A screen that only remembered his own searches would go blank overnight
    and say nothing about what arrived while he slept."""
    _profile(con)

    async def found(profile):
        return {"new": 5, "duplicate": 0, "known": 2}

    monkeypatch.setattr(polling, "poll_profile", found)
    monkeypatch.setattr(polling, "_profile_due", lambda p, now: True)
    await polling.poll_all_profiles(force=False)

    with db.db() as fresh:
        assert polling.last_poll(fresh)["by"] == "schedule"
        assert polling.last_poll(fresh)["new"] == 5


async def test_a_tick_with_nothing_due_keeps_the_last_real_receipt(
        con, data_dir, monkeypatch):
    """The scheduler wakes every five minutes. Overwriting the receipt with
    three zeros each time would answer "what did the last search find?" with
    "nothing", for ever."""
    _profile(con)

    async def found(profile):
        return {"new": 4, "duplicate": 0, "known": 1}

    monkeypatch.setattr(polling, "poll_profile", found)
    await polling.poll_all_profiles(force=True)

    monkeypatch.setattr(polling, "_profile_due", lambda p, now: False)
    await polling.poll_all_profiles(force=False)

    with db.db() as fresh:
        assert polling.last_poll(fresh)["new"] == 4


async def test_two_passes_never_run_at_once(con, data_dir, monkeypatch):
    """A press landing on top of the scheduled pass would send every query
    twice — to an API this project already uses on sufferance."""
    _profile(con)
    overlapping = []
    inside = {"n": 0}

    async def slow(profile):
        inside["n"] += 1
        overlapping.append(inside["n"])
        await asyncio.sleep(0.05)
        inside["n"] -= 1
        return {"new": 1, "duplicate": 0, "known": 0}

    monkeypatch.setattr(polling, "poll_profile", slow)
    await asyncio.gather(polling.poll_all_profiles(force=True),
                         polling.poll_all_profiles(force=True))

    assert max(overlapping) == 1, "two passes were in flight at the same time"


async def test_the_button_can_tell_whether_a_pass_is_under_way(
        con, data_dir, monkeypatch):
    _profile(con)
    seen = []

    async def slow(profile):
        seen.append(polling.running())
        return {"new": 0, "duplicate": 0, "known": 0}

    monkeypatch.setattr(polling, "poll_profile", slow)
    assert polling.running() is False
    await polling.poll_all_profiles(force=True)

    assert seen == [True]
    assert polling.running() is False


@pytest.mark.parametrize("stored", ["", "kaputt", "[]", "null", '{"new": "x"}'])
def test_an_unreadable_receipt_never_takes_the_page_down(con, data_dir, stored):
    """These are rows in a table he can edit, and they are read while a page is
    being BUILT — the same shape as the non-finite age threshold that once took
    down the inbox and the settings page that could fix it."""
    db.set_setting(con, polling.LAST_POLL_REPORT, stored)
    con.commit()

    report = polling.last_poll(con)

    assert report["new"] == 0 and report["profiles"] == 0
