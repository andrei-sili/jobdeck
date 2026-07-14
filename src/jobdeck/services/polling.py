"""Profile polling: query all sources in parallel, dedupe, store new jobs.

Every sqlite call is pushed through asyncio.to_thread so the event loop
(shared with the UI) never blocks. A failing source degrades gracefully:
its error lands on the profile row for the UI banner while the remaining
sources keep delivering.
"""

import asyncio
import datetime
import json
import logging

import httpx

from jobdeck import db
from jobdeck.dedupe import find_duplicate_bewerbung, find_duplicate_job
from jobdeck.sources import get_sources
from jobdeck.sources.base import JobPosting, SearchQuery, SourceUnavailable

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def http_client() -> httpx.AsyncClient:
    """Shared client with sane timeouts and light retries."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=20.0,
            transport=httpx.AsyncHTTPTransport(retries=2),
            headers={"User-Agent": "jobdeck (+https://github.com/andrei-sili/jobdeck)"},
        )
    return _client


def _profile_due(profile, now: datetime.datetime) -> bool:
    if not profile["active"]:
        return False
    last = profile["last_polled_at"]
    if not last:
        return True
    try:
        last_dt = datetime.datetime.fromisoformat(last)
    except ValueError:
        return True
    interval = datetime.timedelta(minutes=profile["poll_interval_min"] or 60)
    return now - last_dt >= interval


def _store_posting(profile_id: int, posting: JobPosting) -> str:
    """Insert one posting with duplicate handling. Returns the outcome:
    'new', 'duplicate' (already applied at this company), or 'known'."""
    with db.db() as con:
        if find_duplicate_job(con, posting.company, posting.title):
            return "known"  # same job already arrived through another source
        dup = find_duplicate_bewerbung(con, posting.company, posting.contact_email)
        values = {
            "profile_id": profile_id,
            "source": posting.source,
            "external_id": posting.external_id,
            "title": posting.title,
            "company": posting.company,
            "location": posting.location,
            "remote": posting.remote,
            "url": posting.url,
            "description": posting.description,
            "contact_email": posting.contact_email,
            "published_at": posting.published_at,
        }
        if dup is not None:
            values["status"] = "duplicate"
            values["duplicate_of"] = dup["id"]
        job_id = db.insert_job_if_new(con, values)
        if job_id is None:
            return "known"
        return "duplicate" if dup is not None else "new"


async def poll_profile(profile) -> dict[str, int]:
    """Poll one profile across its sources. Returns outcome counters."""
    sources = get_sources(http_client())
    wanted = json.loads(profile["sources"] or "[]")
    query = SearchQuery(
        keywords=profile["keywords"],
        location=profile["location"] or "",
        radius_km=profile["radius_km"] or 0,
    )
    results = await asyncio.gather(
        *(sources[name].search(query) for name in wanted if name in sources),
        return_exceptions=True,
    )

    counters = {"new": 0, "duplicate": 0, "known": 0}
    errors: list[str] = []
    for outcome in results:
        if isinstance(outcome, SourceUnavailable):
            errors.append(str(outcome))
            continue
        if isinstance(outcome, BaseException):
            log.exception("poll failed", exc_info=outcome)
            errors.append(str(outcome))
            continue
        for posting in outcome:
            # Enrich before storing so dedupe sees the contact email.
            if not posting.description:
                source = sources.get(posting.source)
                if source is not None:
                    posting = await source.fetch_details(posting)
            result = await asyncio.to_thread(_store_posting, profile["id"], posting)
            counters[result] += 1

    error_text = "; ".join(errors) if errors else None
    await asyncio.to_thread(_mark_polled, profile["id"], error_text)
    log.info(
        "profile %s: %d new, %d duplicate, %d known%s",
        profile["name"], counters["new"], counters["duplicate"], counters["known"],
        f", errors: {error_text}" if error_text else "",
    )
    return counters


def _mark_polled(profile_id: int, error: str | None) -> None:
    with db.db() as con:
        db.mark_profile_polled(con, profile_id, error)


async def poll_all_profiles(force: bool = False) -> dict[str, int]:
    """Poll every active profile that is due (or all, when forced)."""
    now = datetime.datetime.now()
    profiles = await asyncio.to_thread(_list_active_profiles)
    total = {"new": 0, "duplicate": 0, "known": 0}
    for profile in profiles:
        if force or _profile_due(profile, now):
            counters = await poll_profile(profile)
            for key, value in counters.items():
                total[key] += value
    return total


def _list_active_profiles():
    with db.db() as con:
        return db.list_profiles(con, active_only=True)
