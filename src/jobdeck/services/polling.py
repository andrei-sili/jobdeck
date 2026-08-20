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

# One pass at a time, whoever asked for it. The scheduler wakes every profile
# hourly and there is now a button on the Stellen screen; without this, a press
# landing on top of the scheduled pass sends every query twice — to an API this
# project already uses on sufferance.
_lock = asyncio.Lock()

# Where the last pass writes down what it found. `poll_all_profiles` has always
# RETURNED these three numbers and thrown them into a log line, so a search that
# found nothing looked exactly like a search that never ran — which is half of
# what he was asking when he said "cind? cum?".
#
# Stored rather than notified: a toast answers the question for whoever happened
# to be looking, and he asks it precisely when he was not.
LAST_POLL_AT = "last_poll_at"
LAST_POLL_REPORT = "last_poll_report"
LAST_POLL_SOURCE = "last_poll_source"


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
        db.set_job_facts(con, job_id, posting.facts)
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
    """Poll every active profile that is due (or all, when forced).

    `force` is what a press means and `due` is what the schedule means, and the
    two are the same pass — so the report is written on both paths. A screen
    that only remembered his own searches would go blank overnight and say
    nothing about the twelve postings that arrived while he slept.
    """
    async with _lock:  # a press landing on the scheduled pass is one pass
        now = datetime.datetime.now()
        profiles = await asyncio.to_thread(_list_active_profiles)
        total = {"new": 0, "duplicate": 0, "known": 0, "profiles": 0}
        for profile in profiles:
            if force or _profile_due(profile, now):
                counters = await poll_profile(profile)
                total["profiles"] += 1
                for key, value in counters.items():
                    total[key] += value
        # …but only when a pass actually ran. A scheduler tick that found
        # nothing due must not overwrite the receipt of the last real search
        # with three zeros.
        if total["profiles"]:
            await asyncio.to_thread(_record_poll, total, force)
        return total


def running() -> bool:
    """Whether a pass is under way — for a button that must not offer a second."""
    return _lock.locked()


def _record_poll(total: dict[str, int], force: bool) -> None:
    with db.db() as con:
        db.set_setting(con, LAST_POLL_AT, datetime.datetime.now().isoformat())
        db.set_setting(con, LAST_POLL_REPORT, json.dumps(total))
        db.set_setting(con, LAST_POLL_SOURCE, "user" if force else "schedule")


def _whole(value) -> int:
    """One figure out of a stored report, 0 for anything that is not one.

    Screened per FIGURE rather than per report: a single unparseable value
    would otherwise blank all four, and the report is read while a page is
    being built — the shape that once took down the inbox AND the settings
    page that could have fixed it.
    """
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def last_poll(con) -> dict:
    """What the last pass found, for the line above the list.

    Answers with empty rather than raising on anything unparseable: these are
    rows in a table he can edit, and they are read while a page is being built.
    """
    raw = db.get_setting(con, LAST_POLL_REPORT, "")
    try:
        report = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        report = {}
    if not isinstance(report, dict):
        report = {}
    counts = {key: _whole(report.get(key))
              for key in ("new", "duplicate", "known", "profiles")}
    return {
        "at": db.get_setting(con, LAST_POLL_AT, ""),
        "by": db.get_setting(con, LAST_POLL_SOURCE, ""),
        **counts,
    }


def _list_active_profiles():
    with db.db() as con:
        return db.list_profiles(con, active_only=True)
