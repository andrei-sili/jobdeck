"""Arbeitnow adapter.

Free, keyless JSON feed of German tech/startup jobs pulled directly from
company ATSes (Greenhouse, Recruitee, Join.com, ...). Strong on remote
tech roles. The feed is unfiltered, so keyword/location matching happens
client-side.
"""

import logging
import re

import httpx

from jobdeck.dedupe import fold
from jobdeck.sources.base import (
    JobPosting,
    SearchQuery,
    SourceUnavailable,
    extract_email,
    strip_html,
)

log = logging.getLogger(__name__)

FEED_URL = "https://www.arbeitnow.com/api/job-board-api"
MAX_PAGES = 3  # newest ~300 postings per poll; older pages rarely change


# What this board calls an offered training position — in `job_types`, its
# own classification ("Working student", "Intern"), and in the title. Word
# anchored, so "intern" cannot read "International".
_TRAINING = re.compile(
    r"\bwerkstudent|\bworking[\s-]*student|\bintern(?:ship|s)?\b|\bpraktik"
    r"|\btrainee\b|\bapprentice|\bausbildung\b|\bazubi\b"
    r"|\bduale[snm]?\s+stud|\bstudentische",
    re.I,
)


def offers_training(item: dict) -> bool:
    """Whether the board itself, or the title, says this is a training
    position. Read only when the candidate's own rules exclude those — the
    query carries that decision, the adapter never makes it."""
    texts = [str(item.get("title", "") or "")]
    texts.extend(str(kind) for kind in (item.get("job_types") or []))
    return any(_TRAINING.search(text) for text in texts)


class ArbeitnowSource:
    name = "arbeitnow"

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    def _matches(self, query: SearchQuery, item: dict) -> bool:
        # Title and tags only, never the body. Measured on the 1288 scored
        # postings this feed had delivered (2026-09-09): the 380 whose title
        # named no developer role had matched the keyword somewhere in the
        # body and produced ONE score above 60 and none above 80; the 908
        # that did produced 114 and 13. The body is where a robotics,
        # data-labelling or flight-test advert mentions the language in
        # passing. `fold`, not `norm`: a search haystack keeps every character.
        haystack = fold(
            " ".join(
                [
                    item.get("title", "") or "",
                    " ".join(str(tag) for tag in item.get("tags", []) or []),
                ]
            )
        )
        terms = [t for t in fold(query.keywords).split() if t]
        if terms and not any(term in haystack for term in terms):
            return False
        if query.exclude_training and offers_training(item):
            return False
        if query.location:
            location_ok = fold(item.get("location", "")).find(fold(query.location)) >= 0
            if not location_ok and not item.get("remote", False):
                return False
        return True

    async def search(self, query: SearchQuery) -> list[JobPosting]:
        postings: list[JobPosting] = []
        for page in range(1, MAX_PAGES + 1):
            try:
                resp = await self._client.get(FEED_URL, params={"page": page})
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as ex:
                if page == 1:
                    raise SourceUnavailable(self.name, str(ex)) from ex
                break  # partial results are fine past page 1
            items = payload.get("data", []) or []
            if not items:
                break
            for item in items:
                try:
                    if not self._matches(query, item):
                        continue
                    slug = item.get("slug", "")
                    if not slug:
                        continue
                    description = strip_html(item.get("description", "") or "")
                    postings.append(
                        JobPosting(
                            source=self.name,
                            external_id=slug,
                            title=item.get("title", "") or "",
                            company=item.get("company_name", "") or "",
                            location=item.get("location", "") or "",
                            remote=bool(item.get("remote", False)),
                            url=item.get("url", "") or "",
                            description=description,
                            contact_email=extract_email(description),
                            published_at=str(item.get("created_at", "") or ""),
                            raw=item,
                        )
                    )
                except (AttributeError, TypeError) as ex:
                    log.warning("arbeitnow: skipping malformed item: %s", ex)
        return postings

    async def fetch_details(self, posting: JobPosting) -> JobPosting:
        return posting  # the feed already carries the full description
