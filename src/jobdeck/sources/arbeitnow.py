"""Arbeitnow adapter.

Free, keyless JSON feed of German tech/startup jobs pulled directly from
company ATSes (Greenhouse, Recruitee, Join.com, ...). Strong on remote
tech roles. The feed is unfiltered, so keyword/location matching happens
client-side.
"""

import logging

import httpx

from jobdeck.dedupe import norm
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


class ArbeitnowSource:
    name = "arbeitnow"

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    def _matches(self, query: SearchQuery, item: dict) -> bool:
        haystack = norm(
            " ".join(
                [
                    item.get("title", "") or "",
                    " ".join(item.get("tags", []) or []),
                    item.get("description", "") or "",
                ]
            )
        )
        terms = [t for t in norm(query.keywords).split() if t]
        if terms and not any(term in haystack for term in terms):
            return False
        if query.location:
            location_ok = norm(item.get("location", "")).find(norm(query.location)) >= 0
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
