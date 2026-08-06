"""Is that ad still online?

47% of his best form/portal postings were already taken down when measured
(2026-08-05): of the top 30 by score, 14 answered HTTP 404, aged 24-155 days.
One of them had a draft AND a 2.1 MB Bewerbungsmappe built for a posting gone
40 days. Nothing in the pipeline ever expired, because discovery only ever
stores what is new.

The signal is free and already in reach for two of the three sources:

* **arbeitsagentur** — the detail endpoint the poller already uses answers 404
  once an ad is withdrawn. An API call, not a crawl.
* **arbeitnow** — the job page answers 404/410; robots.txt allows exactly that
  route (it disallows `…/apply`, which is never touched here).
* **jooble** — robots.txt disallows every URL a result points at, so there is
  NO probe to make. Those postings keep `liveness` empty and age is the only
  freshness signal they will ever have. Stating that plainly is the honest
  answer; inventing a fetch there would not be.

A posting is only ever marked `gone` on an explicit "not here" from the server.
Anything else — a timeout, a 5xx, a refused hop, a 405 to HEAD — is UNKNOWN and
changes nothing: a hidden posting is a posting he does not see, so the cost of
guessing wrong points one way only.

Being marked gone HIDES a posting behind a toggle. It never deletes it, never
touches its status, and never touches a draft or an application built from it.
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx

from jobdeck import apply_channel, db, netsafe
from jobdeck.constants import LIVENESS_ALIVE, LIVENESS_GONE
from jobdeck.sources import arbeitsagentur

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Probe:
    """What one probe learned. `verdict` is 'alive', 'gone', or None for "the
    server did not answer the question". `published_raw` carries a publication
    date the source volunteered in the same response — freshness is only as
    honest as that date, and this is the one request that already has it."""

    verdict: str | None
    published_raw: str = ""


# "This posting is not here." Both are used in the wild: the BA API answers 404,
# Arbeitnow answers 410 Gone for a withdrawn ad.
GONE_STATUSES = (404, 410)

_USER_AGENT = "JobDeck/0.1 (+https://github.com/andrei-sili/jobdeck)"
_TIMEOUT = 10.0
_MAX_REDIRECTS = 10

BATCH_LIMIT = 200          # the whole probeable corpus fits in one pass today
BATCH_PAUSE_S = 0.5        # between postings — this walks other people's sites
RECHECK_AFTER_H = 20       # once a day per posting, whatever the tick interval
PROBE_DEADLINE_S = 25.0    # per posting, wall clock

# A module-level lock makes passes single-flight, like the scoring service's:
# the Settings button would otherwise overlap the scheduled run and ask other
# people's servers the same 200 questions twice at once.
_lock = asyncio.Lock()


def _verdict(status: int | None) -> str | None:
    """'gone', 'alive', or None for "the server did not answer the question"."""
    if status is None:
        return None
    if status in GONE_STATUSES:
        return LIVENESS_GONE
    if status == 200:
        return LIVENESS_ALIVE
    return None  # 3xx exhausted, 401/403/405, 5xx — no statement either way


async def _probe_arbeitsagentur(job, client: httpx.AsyncClient) -> Probe:
    """Ask the BA API whether the Referenznummer still resolves.

    A first-party endpoint of ours with a fixed host, so it goes direct like
    every other call in the adapter; netsafe guards board-DERIVED URLs, and this
    one is built from a constant plus an id.

    A live answer also carries the current publication period, so this pass
    corrects the posting's age for free: a re-published ad is fresh however long
    ago it first appeared (see arbeitsagentur.publication_start)."""
    external_id = (job["external_id"] or "").strip()
    if not external_id:
        return Probe(None)
    try:
        resp = await client.get(
            arbeitsagentur.detail_url(external_id),
            headers={"X-API-Key": arbeitsagentur.API_KEY},
        )
    except httpx.HTTPError as exc:
        log.info("liveness: arbeitsagentur %s unreachable: %s", external_id, exc)
        return Probe(None)
    verdict = _verdict(resp.status_code)
    if verdict != LIVENESS_ALIVE:
        return Probe(verdict)
    try:
        payload = resp.json()
    except ValueError:  # the endpoint has changed shape before — liveness stands
        return Probe(verdict)
    return Probe(verdict, arbeitsagentur.publication_start(payload))


async def _probe_arbeitnow(job, client: httpx.AsyncClient) -> Probe:
    """HEAD the job page — the one route of theirs robots.txt allows.

    Deliberately the stored `url` and not the resolved `apply_url`: resolution
    may have turned the latter into the `…/apply` deep-link, which robots.txt
    disallows and which this app must never request. The same rule is handed to
    the probe as a per-hop refusal, because a redirect into that route would be
    the board's choice and still our request. A HEAD carries no publication
    date, and the feed's own `created_at` is already read at ingestion."""
    url = (job["url"] or "").strip()
    if not apply_channel.is_arbeitnow_job(url):
        return Probe(None)
    return Probe(_verdict(await netsafe.probe_status(
        client, url, max_redirects=_MAX_REDIRECTS,
        refuse=apply_channel.is_robots_disallowed)))


_PROBES = {
    "arbeitsagentur": _probe_arbeitsagentur,
    "arbeitnow": _probe_arbeitnow,
    # jooble: no probe exists — see the module docstring
}


def is_probeable(source: str) -> bool:
    """True when this source can be asked at all."""
    return source in _PROBES


async def probe(job, client: httpx.AsyncClient) -> Probe:
    """What one posting's source says about it. An unaskable source answers
    with an empty Probe rather than a guess."""
    prober = _PROBES.get(job["source"])
    if prober is None:
        return Probe(None)
    return await prober(job, client)


def _pending(limit: int):
    with db.db() as con:
        return [dict(r) for r in db.jobs_needing_liveness_check(
            con, limit, sources=tuple(_PROBES), recheck_after_h=RECHECK_AFTER_H)]


def _store(job_id: int, result: Probe) -> None:
    with db.db() as con:
        db.set_job_liveness(con, job_id, result.verdict)
        if result.published_raw:
            db.refresh_job_published_on(con, job_id, result.published_raw)


async def check_pending(limit: int | None = None,
                        client: httpx.AsyncClient | None = None) -> dict:
    """Probe a batch of postings, best-scored first, and record what came back.

    Bounded five ways so this can run unattended: one pass at a time, `limit`
    postings per pass, one posting at a time with a pause between them,
    `PROBE_DEADLINE_S` of wall clock per posting, and nothing re-probed inside
    `RECHECK_AFTER_H`. Never raises, and never lets one posting end the pass —
    not the probe, and not the write that records it. `client` is injectable so
    a test drives the whole pass through a MockTransport rather than the network.

    A source that volunteers a publication date in the same answer also gets the
    posting's age corrected, which is how existing rows stop being judged by the
    date their ad FIRST appeared.
    """
    # read at CALL time, not captured as a default: BATCH_LIMIT is a promise to
    # other people's servers, and a default argument would freeze the value this
    # module was imported with
    limit = BATCH_LIMIT if limit is None else limit
    counts = {LIVENESS_ALIVE: 0, LIVENESS_GONE: 0, "unknown": 0}
    if _lock.locked():
        # Not a queue: a second pass would ask the same servers the same
        # questions at the same time, and the first one is already asking.
        log.info("liveness: a pass is already running — skipping this one")
        return {"checked": 0, **counts, "redated": 0}
    async with _lock:
        jobs = await asyncio.to_thread(_pending, limit)
        if not jobs:
            return {"checked": 0, **counts, "redated": 0}
        redated = 0
        owned = client is None
        if owned:
            client = httpx.AsyncClient(
                headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT,
                max_redirects=_MAX_REDIRECTS,
            )
        try:
            for index, job in enumerate(jobs):
                if index:
                    await asyncio.sleep(BATCH_PAUSE_S)
                try:
                    # an httpx timeout is per-operation and bounds neither a
                    # redirect chain nor a server trickling one byte at a time;
                    # this job is max_instances=1, so one such posting would
                    # hold the only slot indefinitely
                    async with asyncio.timeout(PROBE_DEADLINE_S):
                        result = await probe(job, client)
                except Exception as exc:  # noqa: BLE001 - one posting, not the pass
                    log.warning("liveness: job %s failed: %s", job["id"], exc)
                    result = Probe(None)
                counts["unknown" if result.verdict is None else result.verdict] += 1
                redated += bool(result.published_raw)
                # an unanswered probe is still recorded as an ATTEMPT: without
                # the timestamp a permanently unreachable posting sits at the
                # head of the queue (oldest check first) and starves the rest
                try:
                    await asyncio.to_thread(_store, job["id"], result)
                except Exception as exc:  # noqa: BLE001 - a locked DB, one row
                    log.warning("liveness: storing job %s failed: %s",
                                job["id"], exc)
        finally:
            if owned:
                await client.aclose()
    log.info("liveness batch: %s checked, %s alive, %s gone, %s unanswered, "
             "%s dates refreshed", len(jobs), counts[LIVENESS_ALIVE],
             counts[LIVENESS_GONE], counts["unknown"], redated)
    return {"checked": len(jobs), **counts, "redated": redated}
