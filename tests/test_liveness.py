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
    """The verdict alone — the date a probe may also carry has its own tests."""
    async with _client(handler) as client:
        return (await liveness.probe(job, client)).verdict


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

    assert res == {"checked": 3, "alive": 1, "gone": 1, "unknown": 1,
                   "redated": 0}
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

    assert res == {"checked": 2, "alive": 1, "gone": 0, "unknown": 1,
                   "redated": 0}
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


_DETAIL = {
    "stellenangebotsBeschreibung": "Wir suchen…",
    # the two dates the API states, and they mean different things
    "datumErsteVeroeffentlichung": "2025-01-28",
    "veroeffentlichungszeitraum": {"von": "2026-07-08"},
}


async def test_a_live_ba_answer_also_corrects_the_age_of_the_ad():
    """The answer that proves an ad is alive says when its CURRENT version went
    up. One of his own postings reads 555 days old by first publication while
    the ad in front of him is 29 days old."""
    def handler(request):
        return httpx.Response(200, json=_DETAIL)

    async with _client(handler) as client:
        result = await liveness.probe(_BA_JOB, client)
    assert result.verdict == liveness.LIVENESS_ALIVE
    assert result.published_raw == "2026-07-08"   # not 2025-01-28


async def test_a_dead_or_mute_answer_never_carries_a_date():
    """Nothing may be inferred about an ad that did not answer — and the body is
    a FULL detail payload, so the guard is what stops the date rather than the
    absence of anything to read."""
    status = {"code": 0}

    def handler(request):
        return httpx.Response(status["code"], json=_DETAIL)

    for code in (404, 410, 403, 500, 503):
        status["code"] = code
        async with _client(handler) as client:
            result = await liveness.probe(_BA_JOB, client)
        assert result.published_raw == "", code
        assert result.verdict != liveness.LIVENESS_ALIVE, code


async def test_a_detail_payload_without_the_period_falls_back_and_never_raises():
    def only_first(request):
        return httpx.Response(200, json={"datumErsteVeroeffentlichung": "2025-01-28"})

    def junk(request):
        return httpx.Response(200, json={"veroeffentlichungszeitraum": "kaputt"})

    def not_json(request):
        return httpx.Response(200, text="<html>maintenance</html>")

    async with _client(only_first) as client:
        assert (await liveness.probe(_BA_JOB, client)).published_raw == "2025-01-28"
    for handler in (junk, not_json):
        async with _client(handler) as client:
            result = await liveness.probe(_BA_JOB, client)
        # the shape has changed before: liveness still stands, the date does not
        assert result.verdict == liveness.LIVENESS_ALIVE
        assert result.published_raw == ""


async def test_the_pass_stores_the_corrected_date_and_counts_it(con, data_dir,
                                                               monkeypatch):
    monkeypatch.setattr(liveness, "BATCH_PAUSE_S", 0)
    ids = _seed(con, [{"source": "arbeitsagentur", "ext": "old",
                       "url": _BA_JOB["url"]}])
    con.execute("UPDATE jobs SET published_at='2025-01-28', "
                "published_on='2025-01-28' WHERE id=?", (ids["old"],))
    con.commit()

    def handler(request):
        return httpx.Response(200, json=_DETAIL)

    async with _client(handler) as client:
        res = await liveness.check_pending(limit=10, client=client)

    assert res["alive"] == 1 and res["redated"] == 1
    row = con.execute("SELECT * FROM jobs").fetchone()
    assert row["published_on"] == "2026-07-08"
    # the raw value stays as the SEARCH payload sent it, so the backfill's
    # fill-blanks-only rule still holds and the two remain comparable
    assert row["published_at"] == "2025-01-28"


def test_refreshing_a_date_reports_whether_anything_moved(con, data_dir):
    ids = _seed(con, [{"source": "arbeitsagentur", "ext": "j", "url": _BA_JOB["url"]}])
    job_id = ids["j"]
    assert db.refresh_job_published_on(con, job_id, "2026-07-08") is True
    assert db.refresh_job_published_on(con, job_id, "2026-07-08") is False
    assert db.refresh_job_published_on(con, job_id, "irgendwann") is False
    assert db.get_job(con, job_id)["published_on"] == "2026-07-08"


async def test_a_redirect_into_the_disallowed_apply_route_is_refused():
    """robots.txt Disallows /jobs/companies/*/apply. A 3xx into it is the
    board's choice and still our request, so the rule is checked per hop."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path.endswith("/apply"):
            return httpx.Response(200)   # must never be reached
        return httpx.Response(302, headers={"Location": _AN_JOB["url"] + "/apply"})

    async with _client(handler) as client:
        result = await liveness.probe(_AN_JOB, client)
    assert seen == [_AN_JOB["url"]]      # the disallowed hop never fired
    assert result.verdict is None        # and no verdict was invented from it


async def test_a_stored_apply_url_is_not_probeable_at_all():
    # defence in depth: if that deep-link ever reached the `url` column, the
    # probe must refuse it rather than fetch a disallowed route
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200)

    job = {**_AN_JOB, "url": _AN_JOB["url"] + "/apply"}
    assert (await _probe(job, handler)) is None
    assert seen == []


async def test_two_passes_never_run_at_once(con, data_dir, monkeypatch):
    """The pass is reachable from the scheduler AND the Settings button. Two of
    them would ask other people's servers the same 200 questions at once."""
    import asyncio
    monkeypatch.setattr(liveness, "BATCH_PAUSE_S", 0)
    _seed(con, [{"source": "arbeitnow", "ext": f"j{n}", "url": _AN_JOB["url"] + str(n)}
                for n in range(3)])
    started = 0

    async def slow_probe(job, client):
        nonlocal started
        started += 1
        await asyncio.sleep(0.05)
        return liveness.Probe(liveness.LIVENESS_ALIVE)

    monkeypatch.setattr(liveness, "probe", slow_probe)
    async with _client(_status(200)) as client:
        first, second = await asyncio.gather(
            liveness.check_pending(limit=10, client=client),
            liveness.check_pending(limit=10, client=client),
        )

    ran, skipped = sorted((first, second), key=lambda r: -r["checked"])
    assert ran["checked"] == 3 and ran["alive"] == 3
    assert skipped["checked"] == 0        # not queued behind it — skipped
    assert started == 3                  # every posting asked exactly once


async def test_the_lock_is_released_even_when_a_pass_blows_up(con, data_dir,
                                                             monkeypatch):
    # a lock held after a crash would silence the pass for the whole process
    monkeypatch.setattr(liveness, "BATCH_PAUSE_S", 0)
    _seed(con, [{"source": "arbeitnow", "ext": "j", "url": _AN_JOB["url"]}])

    real_pending = liveness._pending

    def exploding(limit):
        raise RuntimeError("the database is on fire")

    # restored by hand rather than with monkeypatch.undo(), which would also
    # undo the data_dir fixture and point the service at the REAL database
    monkeypatch.setattr(liveness, "_pending", exploding)
    try:
        await liveness.check_pending(limit=10)
    except RuntimeError:
        pass
    assert not liveness._lock.locked()

    monkeypatch.setattr(liveness, "_pending", real_pending)
    async with _client(_status(200)) as client:
        assert (await liveness.check_pending(limit=10, client=client))["checked"] == 1


async def test_only_an_arbeitnow_job_url_is_ever_fetched_for_an_arbeitnow_row():
    """The `url` comes verbatim from a third-party feed, so the screen in front
    of the fetch is the load-bearing part. A row whose source says arbeitnow but
    whose URL points anywhere else must produce no request at all."""
    asked = []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(200)

    foreign = [
        "https://de.jooble.org/away/123",              # robots-disallowed elsewhere
        "https://evil.example/jobs/companies/x/y",     # a lookalike path, wrong host
        "https://www.arbeitnow.com/companies/x/y",     # right host, not a job route
        "https://www.arbeitnow.com/jobs/companies/x/y/apply",   # the forbidden route
        "http://[::1",                                 # malformed must not raise
        "",
    ]
    for url in foreign:
        async with _client(handler) as client:
            result = await liveness.probe({**_AN_JOB, "url": url}, client)
        assert result.verdict is None, url
    assert asked == []


def test_a_posting_he_has_committed_to_is_still_asked_about(con, data_dir):
    """A posting whose form he opened and has not confirmed yet is the exact
    moment "the ad is gone" is worth five minutes of his time, and it is what
    the review queue's pre-send warning depends on. 'skipped'/'applied'/
    'duplicate' are finished business.

    The started posting is deliberately seeded the way v10 writes it — status
    'new' plus the timestamp — and the assertion below names it, so this test
    keeps covering what it claims to rather than passing through the 'new' arm
    by accident."""
    ids = _seed(con, [
        {"source": "arbeitnow", "ext": "working", "url": _AN_JOB["url"], "score": 80},
        {"source": "arbeitnow", "ext": "at-the-form", "url": _AN_JOB["url"] + "2",
         "score": 90},
        {"source": "arbeitnow", "ext": "given-up", "url": _AN_JOB["url"] + "3",
         "score": 95, "status": "skipped"},
        {"source": "arbeitnow", "ext": "sent", "url": _AN_JOB["url"] + "4",
         "score": 99, "status": "applied"},
    ])
    db.mark_form_opened(con, ids["at-the-form"])
    con.commit()

    queued = [r["external_id"] for r in db.jobs_needing_liveness_check(
        con, limit=10, sources=("arbeitnow",), recheck_after_h=20)]
    assert queued == ["at-the-form", "working"]   # best-scored first among these
    # and it is still marked as started — being probed did not undo his work
    assert db.get_job(con, ids["at-the-form"])["form_opened_at"] != ""


async def test_the_pre_send_warning_survives_pressing_apply_via_portal(
    con, data_dir, monkeypatch
):
    """The queue warning added for the job-18 incident must not be switched off
    by the very button the branch encourages him to press."""
    monkeypatch.setattr(liveness, "BATCH_PAUSE_S", 0)
    ids = _seed(con, [{"source": "arbeitnow", "ext": "j", "url": _AN_JOB["url"]}])
    db.mark_form_opened(con, ids["j"])
    db.upsert_draft(con, ids["j"], {"status": "ready", "recipient": "hr@firma.de",
                                    "betreff": "B", "email_body": "e",
                                    "anschreiben_body": "a",
                                    "pdf_path": "/tmp/m.pdf"})
    con.commit()
    started_at = db.get_job(con, ids["j"])["form_opened_at"]

    async with _client(_status(410)) as client:
        res = await liveness.check_pending(limit=10, client=client)

    assert res["gone"] == 1
    row = db.list_drafts_with_jobs(con, ["ready"])[0]
    assert row["job_liveness"] == "gone"      # the warning can still fire
    # and nothing about his work moved — including WHEN he started it, which is
    # what the strip renders as the application's age
    assert db.get_job(con, ids["j"])["form_opened_at"] == started_at


async def test_every_bound_on_the_outbound_volume_is_load_bearing(con, data_dir,
                                                                 monkeypatch):
    """These numbers are a promise to other people's servers, not a preference:
    a batch limit, a pause between postings, and a wall-clock deadline each.
    Without a test any of them can be deleted or raised with the suite green."""
    import asyncio
    _seed(con, [{"source": "arbeitnow", "ext": f"j{n}",
                 "url": _AN_JOB["url"] + str(n), "score": 100 - n}
                for n in range(8)])
    asked, slept = [], []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(200)

    real_sleep = asyncio.sleep

    async def counting_sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(liveness.asyncio, "sleep", counting_sleep)
    monkeypatch.setattr(liveness, "BATCH_LIMIT", 3)
    async with _client(handler) as client:
        res = await liveness.check_pending(client=client)

    # the limit bounds the pass, and it is read at call time rather than frozen
    # into a default argument
    assert res["checked"] == 3 and len(asked) == 3
    # one pause BETWEEN postings, never before the first
    assert slept == [liveness.BATCH_PAUSE_S] * 2

    # and the deadline really wraps each probe
    async def never_answers(request):
        await real_sleep(5)
        return httpx.Response(200)

    monkeypatch.setattr(liveness, "PROBE_DEADLINE_S", 0.05)
    monkeypatch.setattr(liveness, "BATCH_LIMIT", 1)
    async with _client(never_answers) as client:
        res = await liveness.check_pending(client=client)
    assert res == {"checked": 1, "alive": 0, "gone": 0, "unknown": 1, "redated": 0}


def test_the_volume_numbers_are_pinned_as_literals():
    """Pinned as values, not read off the module: the test above proves the pass
    HONOURS these numbers, which stays green if someone raises them tenfold.
    What they bound is how hard an unattended job hits other people's servers,
    so changing one should have to change this line and say why.

    200 per pass covers his whole probeable corpus once; 0.5 s between postings
    is gentler than a human clicking; 20 h means once a day per posting whatever
    the tick interval; 25 s per probe keeps one slow server from holding the
    single-flight slot."""
    assert liveness.BATCH_LIMIT == 200
    assert liveness.BATCH_PAUSE_S == 0.5
    assert liveness.RECHECK_AFTER_H == 20
    assert liveness.PROBE_DEADLINE_S == 25.0


# ---------------------------------------------------------------------------
# The daily probe already holds the detail payload — so the columns the app
# used to throw away fill in for free, on the stock as well as on new rows.
# ---------------------------------------------------------------------------
_DETAIL_WITH_FACTS = {
    **_DETAIL,
    "stellenlokationen": [{"adresse": {
        "strasse": "Musterstraße", "hausnummer": "26",
        "plz": "54321", "ort": "Beispielstadt"}}],
    "gehaltsspanneVon": 37000, "gehaltsspanneBis": 47000,
    "verguetungsangabe": "JAHRESGEHALT",
    "istArbeitnehmerUeberlassung": True,
}


async def test_a_live_answer_also_carries_the_facts_the_payload_states():
    def handler(request):
        return httpx.Response(200, json=_DETAIL_WITH_FACTS)

    async with _client(handler) as client:
        result = await liveness.probe(_BA_JOB, client)

    assert result.facts["work_strasse"] == "Musterstraße 26"
    assert result.facts["salary_to"] == "47000"
    assert result.facts["temp_agency"] == 1


async def test_the_pass_fills_the_columns_of_a_posting_stored_before_them(
        con, data_dir, monkeypatch):
    """283 of his Arbeitsagentur postings predate these columns; this is what
    heals them without a single extra request."""
    monkeypatch.setattr(liveness, "BATCH_PAUSE_S", 0)
    ids = _seed(con, [{"source": "arbeitsagentur", "ext": "old",
                       "url": _BA_JOB["url"]}])

    def handler(request):
        return httpx.Response(200, json=_DETAIL_WITH_FACTS)

    async with _client(handler) as client:
        await liveness.check_pending(limit=10, client=client)

    row = db.get_job(con, ids["old"])
    assert row["work_plz_ort"] == "54321 Beispielstadt"
    assert row["salary_period"] == "JAHRESGEHALT"
    assert row["temp_agency"] == 1


async def test_a_dead_posting_leaves_its_stored_facts_alone(con, data_dir,
                                                            monkeypatch):
    """A 404 says nothing about the pay range it once stated."""
    monkeypatch.setattr(liveness, "BATCH_PAUSE_S", 0)
    ids = _seed(con, [{"source": "arbeitsagentur", "ext": "gone",
                       "url": _BA_JOB["url"]}])
    db.set_job_facts(con, ids["gone"], {"salary_from": "37000"})
    con.commit()

    def handler(request):
        return httpx.Response(404, json=_DETAIL_WITH_FACTS)

    async with _client(handler) as client:
        await liveness.check_pending(limit=10, client=client)

    assert db.get_job(con, ids["gone"])["salary_from"] == "37000"
