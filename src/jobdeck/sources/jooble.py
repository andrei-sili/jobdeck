"""Jooble adapter.

Aggregator with an official free API (key via https://jooble.org/api/about).
Content from StepStone, Indeed, XING and other German boards arrives here
indirectly and legally — we never scrape those boards directly.
"""

import logging

import httpx

from jobdeck import config
from jobdeck.sources.base import (
    JobPosting,
    SearchQuery,
    SourceUnavailable,
    extract_email,
    looks_remote,
    strip_html,
)

log = logging.getLogger(__name__)

BASE_URL = "https://de.jooble.org/api"


class JoobleSource:
    name = "jooble"

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def search(self, query: SearchQuery) -> list[JobPosting]:
        api_key = config.jooble_api_key()
        if not api_key:
            raise SourceUnavailable(self.name, "JOOBLE_API_KEY is not configured")
        body: dict[str, str | int] = {"keywords": query.keywords}
        if query.location:
            body["location"] = query.location
            if query.radius_km:
                body["radius"] = query.radius_km
        try:
            resp = await self._client.post(f"{BASE_URL}/{api_key}", json=body)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as ex:
            # The key is part of the URL by Jooble's design — keep it out of
            # error messages, which end up in logs and the UI.
            detail = str(ex).replace(api_key, "***")
            raise SourceUnavailable(self.name, detail) from ex

        postings: list[JobPosting] = []
        for item in payload.get("jobs", []) or []:
            try:
                external_id = str(item.get("id", "")) or item.get("link", "")
                if not external_id:
                    continue
                title = item.get("title", "") or ""
                snippet = strip_html(item.get("snippet", "") or "")
                postings.append(
                    JobPosting(
                        source=self.name,
                        external_id=external_id,
                        title=title,
                        company=item.get("company", "") or "",
                        location=item.get("location", "") or "",
                        remote=looks_remote(title, snippet),
                        url=item.get("link", "") or "",
                        description=snippet,
                        contact_email=extract_email(snippet),
                        published_at=item.get("updated", "") or "",
                        raw=item,
                    )
                )
            except (AttributeError, TypeError) as ex:
                log.warning("jooble: skipping malformed item: %s", ex)
        return postings

    async def fetch_details(self, posting: JobPosting) -> JobPosting:
        return posting  # Jooble has no details endpoint; the snippet is all we get
