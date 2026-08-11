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
