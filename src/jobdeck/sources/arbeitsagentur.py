"""Bundesagentur für Arbeit Jobsuche adapter.

Germany's largest job database, including small local employers that never
reach commercial boards. The endpoint is the one behind the official
Jobsuche app; it is community-documented (bundesAPI/jobsuche-api), not an
officially sanctioned API, and has changed shape before — hence the
defensive parsing: a malformed item is logged and skipped, never fatal.
"""

import base64
import logging

import httpx

from jobdeck.sources.base import (
    JobPosting,
    SearchQuery,
    SourceUnavailable,
    extract_email,
    looks_remote,
    strip_html,
)

log = logging.getLogger(__name__)

BASE_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
API_KEY = "jobboerse-jobsuche"  # public static client id used by the Jobsuche app
PAGE_SIZE = 100
MAX_PAGE_TEXT = 15_000  # cap for external page text (plenty for LLM drafting)


class ArbeitsagenturSource:
    name = "arbeitsagentur"

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def search(self, query: SearchQuery) -> list[JobPosting]:
        params: dict[str, str | int] = {"was": query.keywords, "size": PAGE_SIZE, "page": 1}
        if query.location:
            params["wo"] = query.location
            if query.radius_km:
                params["umkreis"] = query.radius_km
        try:
            resp = await self._client.get(
                f"{BASE_URL}/pc/v4/jobs",
                params=params,
                headers={"X-API-Key": API_KEY},
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as ex:
            raise SourceUnavailable(self.name, str(ex)) from ex

        postings: list[JobPosting] = []
        for item in payload.get("stellenangebote", []) or []:
            try:
                refnr = item.get("refnr", "")
                if not refnr:
                    continue
                title = item.get("titel") or item.get("beruf") or ""
                ort = (item.get("arbeitsort") or {}).get("ort") or ""
                postings.append(
                    JobPosting(
                        source=self.name,
                        external_id=refnr,
                        title=title,
                        company=item.get("arbeitgeber", "") or "",
                        location=ort,
                        remote=looks_remote(title),
                        url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}",
                        published_at=item.get("aktuelleVeroeffentlichungsdatum", "") or "",
                        raw=item,
                    )
                )
            except (AttributeError, TypeError) as ex:
                log.warning("arbeitsagentur: skipping malformed item: %s", ex)
        return postings

    async def fetch_details(self, posting: JobPosting) -> JobPosting:
        encoded = base64.urlsafe_b64encode(posting.external_id.encode()).decode()
        try:
            resp = await self._client.get(
                f"{BASE_URL}/pc/v4/jobdetails/{encoded}",
                headers={"X-API-Key": API_KEY},
            )
            resp.raise_for_status()
            detail = resp.json()
        except (httpx.HTTPError, ValueError) as ex:
            # Best-effort: the posting stays usable with search-level data.
            log.info("arbeitsagentur: details unavailable for %s: %s",
                     posting.external_id, ex)
            return posting

        # Field name observed live in July 2026; the older community docs
        # still list "stellenbeschreibung", kept as fallback.
        description = (
            detail.get("stellenangebotsBeschreibung", "")
            or detail.get("stellenbeschreibung", "")
            or ""
        )
        # Some partner listings have no BA-hosted text at all: the full
        # posting lives on the employer's own page (externeURL).
        external_url = detail.get("externeURL", "") or detail.get("externeUrl", "") or ""
        if external_url:
            posting.url = external_url
        if not description and external_url:
            description = await self._fetch_page_text(external_url)

        posting.description = description
        posting.contact_email = extract_email(description)
        posting.remote = bool(
            detail.get("homeofficemoeglich")
            or posting.remote
            or looks_remote(posting.title, description)
        )
        if not posting.company:
            posting.company = detail.get("arbeitgeber", "") or detail.get("firma", "") or ""
        return posting

    async def _fetch_page_text(self, url: str) -> str:
        """Best-effort text of the employer's public posting page.

        Server-rendered career pages yield usable text; JS-heavy ones come
        back thin — the UI lets the user paste the posting manually then.
        """
        if not url.startswith("http"):
            url = "https://" + url
        try:
            resp = await self._client.get(url, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as ex:
            log.info("arbeitsagentur: external page unavailable (%s): %s", url, ex)
            return ""
        text = strip_html(resp.text)
        return text[:MAX_PAGE_TEXT]
