"""Tests for the liveness pass — "is that ad still online?".

The rule the whole feature rests on: a posting is marked `gone` ONLY on an
explicit "not here" from the source. Everything else is UNKNOWN, because being
marked gone hides the posting from the inbox.
"""

import httpx

from jobdeck import db
from jobdeck.services import liveness

_BA_JOB = {"source": "arbeitsagentur", "external_id": "10001-1003292975-S",
           "url": "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003292975-S"}
_AN_JOB = {"source": "arbeitnow", "external_id": "acme-dev-1",
           "url": "https://www.arbeitnow.com/jobs/companies/acme/dev-1"}
_UK_JOB = {"source": "arbeitnow", "external_id": "acme-dev-2",
           "url": "https://www.arbeitnow.co.uk/jobs/companies/acme/dev-2"}
_JOOBLE_JOB = {"source": "jooble", "external_id": "j1",
               "url": "https://de.jooble.org/away/123"}


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _status(code):
    def handler(request):
        return httpx.Response(code)
    return handler


async def _probe(job, handler):
    async with _client(handler) as client:
        return await liveness.probe(job, client)


async def test_the_ba_api_404_is_what_makes_a_posting_gone():
    # job 18 (Stretta, score 87) had a draft and a 2.1 MB Mappe built for a
    # posting the API had been answering 404 for 40 days
    assert await _probe(_BA_JOB, _status(404)) == liveness.LIVENESS_GONE
    assert await _probe(_BA_JOB, _status(200)) == liveness.LIVENESS_ALIVE


async def test_the_ba_probe_asks_the_detail_route_with_the_api_key():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("x-api-key")))
        return httpx.Response(200)

    await _probe(_BA_JOB, handler)
    url, key = seen[0]
    # one builder for the base64 detail route, shared with fetch_details
    assert url == (
        "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/"
        "jobdetails/MTAwMDEtMTAwMzI5Mjk3NS1T"
    )
    assert key == "jobboerse-jobsuche"


async def test_an_arbeitnow_410_is_gone_on_both_markets():
    # HTTP 410 Gone is what the board really answers for a withdrawn ad
    assert await _probe(_AN_JOB, _status(410)) == liveness.LIVENESS_GONE
    assert await _probe(_UK_JOB, _status(410)) == liveness.LIVENESS_GONE
    assert await _probe(_UK_JOB, _status(200)) == liveness.LIVENESS_ALIVE


async def test_the_arbeitnow_probe_never_requests_the_disallowed_apply_route():
    """robots.txt Disallows /jobs/companies/*/apply. Resolution may have stored
    exactly that deep-link in apply_url, so the probe must use the job page."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200)

    job = {**_AN_JOB, "apply_url": _AN_JOB["url"] + "/apply"}
    await _probe(job, handler)
    assert seen == [_AN_JOB["url"]]


async def test_a_jooble_posting_is_never_probed_at_all():
    """Every URL a Jooble result points at is robots-disallowed, so there is no
    probe to make — and no fetch may be invented to pretend otherwise."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(404)

    assert await _probe(_JOOBLE_JOB, handler) is None
    assert seen == []
    assert not liveness.is_probeable("jooble")


async def test_nothing_but_an_explicit_not_here_marks_a_posting_gone():
    # 403/405/429/5xx and a network failure are all "no answer": hiding a live
    # posting is the expensive mistake, so uncertainty changes nothing
    for code in (301, 401, 403, 405, 429, 500, 502, 503):
        assert await _probe(_BA_JOB, _status(code)) is None, code

    def broken(request):
        raise httpx.ConnectError("no route to host")

    assert await _probe(_BA_JOB, broken) is None
    assert await _probe(_AN_JOB, broken) is None


def _seed(con, rows):
    ids = {}
    for row in rows:
        job_id = db.insert_job_if_new(con, {
            "source": row["source"], "external_id": row["ext"], "title": "Dev",
            "company": "Firma", "url": row["url"], "description": "d",
            "status": row.get("status", "new"),
        })
        con.execute("UPDATE jobs SET match_score=? WHERE id=?",
                    (row.get("score", 80), job_id))
        if row.get("liveness") or row.get("checked_at"):
            con.execute(
                "UPDATE jobs SET liveness=?, liveness_checked_at=? WHERE id=?",
                (row.get("liveness", ""), row.get("checked_at", ""), job_id))
        ids[row["ext"]] = job_id
    con.commit()
    return ids


async def test_a_pass_records_every_verdict_and_hides_nothing_else(con, data_dir,
                                                                  monkeypatch):
    monkeypatch.setattr(liveness, "BATCH_PAUSE_S", 0)
    _seed(con, [
        {"source": "arbeitsagentur", "ext": "dead", "url": _BA_JOB["url"], "score": 90},
        {"source": "arbeitnow", "ext": "live", "url": _AN_JOB["url"], "score": 80},
        {"source": "arbeitnow", "ext": "mute", "url": _UK_JOB["url"], "score": 70},
        {"source": "jooble", "ext": "unaskable", "url": _JOOBLE_JOB["url"], "score": 60},
    ])

    def handler(request):
        if "arbeitsagentur" in request.url.host:
            return httpx.Response(404)
        if request.url.host.endswith(".co.uk"):
            return httpx.Response(503)   # no statement either way
        return httpx.Response(200)

    async with _client(handler) as client:
        res = await liveness.check_pending(limit=10, client=client)

    assert res == {"checked": 3, "alive": 1, "gone": 1, "unknown": 1}
    stored = {r["external_id"]: (r["liveness"], bool(r["liveness_checked_at"]),
                                 r["status"])
              for r in con.execute("SELECT * FROM jobs")}
    assert stored["dead"] == ("gone", True, "new")     # hidden later, never deleted
    assert stored["live"] == ("alive", True, "new")
    # unanswered: the ATTEMPT is timestamped so the pass rotates on, but no
    # verdict is invented
    assert stored["mute"] == ("", True, "new")
    assert stored["unaskable"] == ("", False, "new")   # never even queued


async def test_an_unanswered_probe_does_not_erase_the_last_real_observation(
    con, data_dir, monkeypatch
):
    monkeypatch.setattr(liveness, "BATCH_PAUSE_S", 0)
    _seed(con, [{"source": "arbeitnow", "ext": "flaky", "url": _AN_JOB["url"],
                 "liveness": "alive", "checked_at": "2020-01-01T00:00:00"}])

    async with _client(_status(500)) as client:
        res = await liveness.check_pending(limit=10, client=client)

    assert res["unknown"] == 1
    row = con.execute("SELECT * FROM jobs").fetchone()
    assert row["liveness"] == "alive"
    assert row["liveness_checked_at"] != "2020-01-01T00:00:00"


async def test_one_failing_posting_does_not_end_the_pass(con, data_dir, monkeypatch):
    monkeypatch.setattr(liveness, "BATCH_PAUSE_S", 0)
    _seed(con, [
        {"source": "arbeitnow", "ext": "boom", "url": _AN_JOB["url"], "score": 90},
        {"source": "arbeitnow", "ext": "fine", "url": _UK_JOB["url"], "score": 80},
    ])
    real = liveness.probe

    async def exploding(job, client):
        if job["external_id"] == "boom":
            raise RuntimeError("something unforeseen")
        return await real(job, client)

    monkeypatch.setattr(liveness, "probe", exploding)
    async with _client(_status(200)) as client:
        res = await liveness.check_pending(limit=10, client=client)

    assert res == {"checked": 2, "alive": 1, "gone": 0, "unknown": 1}
    stored = {r["external_id"]: r["liveness"]
              for r in con.execute("SELECT external_id, liveness FROM jobs")}
    assert stored["fine"] == "alive"


def test_the_queue_skips_what_must_not_be_asked(con, data_dir):
    _seed(con, [
        {"source": "arbeitnow", "ext": "ok", "url": _AN_JOB["url"], "score": 80},
        {"source": "arbeitnow", "ext": "ruled-out", "url": _AN_JOB["url"] + "2",
         "score": 0},
        {"source": "arbeitnow", "ext": "applied", "url": _AN_JOB["url"] + "3",
         "score": 80, "status": "applied"},
        {"source": "jooble", "ext": "unaskable", "url": _JOOBLE_JOB["url"]},
    ])
    queued = [r["external_id"] for r in db.jobs_needing_liveness_check(
        con, limit=10, sources=("arbeitsagentur", "arbeitnow"), recheck_after_h=20)]
    assert queued == ["ok"]


def test_the_queue_takes_the_longest_unchecked_first(con, data_dir):
    _seed(con, [
        {"source": "arbeitnow", "ext": "never", "url": _AN_JOB["url"], "score": 10},
        {"source": "arbeitnow", "ext": "old", "url": _AN_JOB["url"] + "2",
         "score": 90, "liveness": "alive", "checked_at": "2026-01-01T00:00:00"},
        {"source": "arbeitnow", "ext": "recent", "url": _AN_JOB["url"] + "3",
         "score": 99, "liveness": "alive", "checked_at": db._now()},
    ])
    queued = [r["external_id"] for r in db.jobs_needing_liveness_check(
        con, limit=10, sources=("arbeitnow",), recheck_after_h=20)]
    # never-checked first even at score 10; the just-checked one is not re-asked
    assert queued == ["never", "old"]


def test_a_gone_posting_is_re_asked_rarely_rather_than_never(con, data_dir):
    """A board answering 404 to everything for an hour must not hide real
    postings forever — but a withdrawn ad must not cost a request every day."""
    _seed(con, [
        {"source": "arbeitnow", "ext": "fresh-gone", "url": _AN_JOB["url"],
         "liveness": "gone", "checked_at": db._hours_ago(48)},
        {"source": "arbeitnow", "ext": "stale-gone", "url": _AN_JOB["url"] + "2",
         "liveness": "gone", "checked_at": db._hours_ago(24 * 9)},
    ])
    queued = [r["external_id"] for r in db.jobs_needing_liveness_check(
        con, limit=10, sources=("arbeitnow",), recheck_after_h=20)]
    assert queued == ["stale-gone"]


def test_gone_postings_are_counted_per_inbox_view(con, data_dir):
    _seed(con, [
        {"source": "arbeitnow", "ext": "a", "url": _AN_JOB["url"], "liveness": "gone"},
        {"source": "arbeitnow", "ext": "b", "url": _AN_JOB["url"] + "2",
         "liveness": "gone", "status": "skipped"},
        {"source": "arbeitnow", "ext": "c", "url": _AN_JOB["url"] + "3"},
    ])
    assert db.count_gone_jobs(con) == 2
    assert db.count_gone_jobs(con, "new") == 1
