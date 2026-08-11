"""Bundesagentur für Arbeit Jobsuche adapter.

Germany's largest job database, including small local employers that never
reach commercial boards. The endpoint is the one behind the official
Jobsuche app; it is community-documented (bundesAPI/jobsuche-api), not an
officially sanctioned API, and has changed shape before — hence the
defensive parsing: a malformed item is logged and skipped, never fatal.
"""

import asyncio
import base64
import logging
import math

import httpx

from jobdeck import netsafe
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
# Search moved to v6 (v4 and v5 answer 404); the DETAIL route did not move and
# has no v6 — every v6/v5/v2/v1 jobdetails path answers 403 as an unregistered
# route. Verified live 2026-08-05, and it matches bundesAPI/jobsuche-api.
SEARCH_PATH = "/pc/v6/jobs"
DETAIL_PATH = "/pc/v4/jobdetails"
HOME_COUNTRY = "DEUTSCHLAND"
MAX_PAGE_TEXT = 15_000  # cap for external page text (plenty for LLM drafting)
MAX_PAGE_BYTES = 400_000  # raw-HTML cap for the guarded external-page GET


def _screen_external_url(raw) -> str:
    """The employer-supplied externeURL normalized, or '' when unusable.

    The field is UNTRUSTED third-party data that becomes the posting URL the
    UI offers to open and the page the poller fetches, so it must clear both
    gates here at ingestion rather than at a later sink: only an http(s) URL
    with a host (a javascript:/data:/file: value dies), and only a host that
    is not a non-public IP literal — the server-side guard protects the fetch,
    but the button hands the URL to the user's OWN browser, which sits inside
    the network a 192.168.x.x or 169.254.169.254 target aims at. A bare host
    form (www.firma.de/jobs, seen in the wild) gets the https scheme it
    implies."""
    if not isinstance(raw, str):
        return ""
    url = raw.strip()
    parts = netsafe.split_url(url)
    if parts is not None and not parts.scheme:
        url = "https://" + url
    if not netsafe.is_openable(url) or not netsafe.public_literal_host(url):
        return ""
    return url


def detail_url(external_id: str) -> str:
    """The API's detail route for a Referenznummer.

    Public because the liveness pass asks this exact endpoint whether a posting
    still exists — it answers 404 once an ad is taken down, which is the only
    free liveness signal in the whole corpus. One builder, so the base64 detail
    encoding cannot drift between the two callers."""
    encoded = base64.urlsafe_b64encode(external_id.encode()).decode()
    return f"{BASE_URL}{DETAIL_PATH}/{encoded}"


def publication_start(payload) -> str:
    """When the CURRENT version of this ad went up, '' when unstated.

    The API states two dates and they mean different things.
    `datumErsteVeroeffentlichung` is when the posting FIRST appeared, ever;
    `veroeffentlichungszeitraum.von` is when the current publication period
    began. Employers re-publish constantly — 32 of 100 fresh search results
    disagree between the two, and one of his own postings reads as 555 days old
    by first publication while its current ad went up 29 days ago. Freshness
    must use the ad in front of him, or it buries live postings for the age of
    an ad that was replaced.

    Present in both the search and the detail payload, and `bis` is never sent,
    so there is no expiry date to read here — that is what liveness is for."""
    if not isinstance(payload, dict):
        return ""
    period = payload.get("veroeffentlichungszeitraum")
    if isinstance(period, dict):
        start = period.get("von")
        if isinstance(start, str) and start.strip():
            return start.strip()
    first = payload.get("datumErsteVeroeffentlichung")
    return first.strip() if isinstance(first, str) else ""


def _place(item) -> str:
    """City of the first work location, with the country when it is not
    Germany.

    v6 replaced the single `arbeitsort` with a `stellenlokationen` LIST whose
    entries carry `adresse.land`. Both of this adapter's known data bugs live
    here: Austrian AMS listings leaked into a nationwide GERMAN search looking
    exactly like domestic ones. They are not dropped — he may still want a
    Vienna job — but the country now travels with the location, so the user
    and the scorer both see what they are looking at instead of reading
    "Wien" as a German city.
    """
    locations = item.get("stellenlokationen")
    if not isinstance(locations, list) or not locations:
        return ""
    address = (locations[0] or {}).get("adresse") or {}
    city = str(address.get("ort") or "").strip()
    country = str(address.get("land") or "").strip()
    if city and country and country.upper() != HOME_COUNTRY:
        return f"{city}, {country}"
    return city


def _first_address(payload) -> dict:
    """The `adresse` of the first work location, {} when there is none."""
    locations = payload.get("stellenlokationen") if isinstance(payload, dict) else None
    if not isinstance(locations, list) or not locations:
        return {}
    address = (locations[0] or {}).get("adresse")
    return address if isinstance(address, dict) else {}


def _amount(raw) -> str:
    """A stated pay figure as a plain number, '' when it is not one.

    Cents are KEPT: the same field carries 55000.0 for a yearly salary and
    30.32 for an hourly one (measured live, 10 of 40 postings state a range),
    so rounding to whole euros would silently misprice every hourly posting.
    Stored as text, exactly like `published_at` — the board's own statement,
    with the interpretation left to where it is used.
    """
    if isinstance(raw, bool) or raw is None:
        return ""
    try:
        value = float(str(raw).replace(",", ".").strip())
    except (TypeError, ValueError):
        return ""
    # json.loads accepts the non-standard `Infinity`/`NaN` literals, and
    # "1e999" parses to inf, so a board answer really can carry one — and
    # neither `inf <= 0` nor `nan <= 0` is true, so the bound below is not the
    # guard. A figure that is not a finite number is not a pay figure.
    if not math.isfinite(value) or value <= 0:
        return ""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def posting_facts(payload) -> dict:
    """Structured facts of a detail payload, as the jobs table stores them.

    Everything here was already being fetched and thrown away. It is a pure
    function of the payload so the two callers that hold one — discovery and
    the daily liveness probe — can both fill these columns, which is what lets
    the existing stock heal without a single extra request.
    """
    if not isinstance(payload, dict):
        return {}
    address = _first_address(payload)
    street = " ".join(part for part in (
        str(address.get("strasse") or "").strip(),
        str(address.get("hausnummer") or "").strip(),
    ) if part)
    plz_ort = " ".join(part for part in (
        str(address.get("plz") or "").strip(),
        str(address.get("ort") or "").strip(),
    ) if part)
    return {
        "work_strasse": street,
        "work_plz_ort": plz_ort,
        "salary_from": _amount(payload.get("gehaltsspanneVon")),
        "salary_to": _amount(payload.get("gehaltsspanneBis")),
        # `verguetungsangabe`, NOT `artDerVerguetung`: probed live over 40 real
        # postings, the latter says what SHAPE the figure has (GEHALTSSPANNE /
        # FESTGEHALT) while the former says what it MEANS (JAHRESGEHALT /
        # STUNDENLOHN) — and a range without that is unreadable, since 30.32
        # and 55000 arrive in the same field.
        "salary_period": str(payload.get("verguetungsangabe") or "").strip(),
        "temp_agency": 1 if payload.get("istArbeitnehmerUeberlassung") else 0,
    }


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
                f"{BASE_URL}{SEARCH_PATH}",
                params=params,
                headers={"X-API-Key": API_KEY},
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as ex:
            raise SourceUnavailable(self.name, str(ex)) from ex

        postings: list[JobPosting] = []
        # v6 renamed the envelope key, and omits it entirely on zero hits
        # rather than sending an empty list.
        for item in payload.get("ergebnisliste") or []:
            try:
                refnr = item.get("referenznummer", "")
                if not refnr:
                    continue
                # `stellenangebotsTitel` is the EMPLOYER'S title;
                # `hauptberuf`/`alleBerufe` are standardised BERUFENET labels.
                # The old fallback to the profession label is what stored 18
                # postings as the generic "Fachinformatiker/in -
                # Anwendungsentwicklung" — losing the one line that says what
                # the job actually is, and feeding that loss straight into the
                # match score. There is deliberately no fallback now: the
                # field was populated in 1600 of 1600 sampled items, and a
                # posting with no title of its own is better skipped than
                # stored under a label that misdescribes it.
                title = item.get("stellenangebotsTitel") or ""
                if not title:
                    continue
                postings.append(
                    JobPosting(
                        source=self.name,
                        external_id=refnr,
                        title=title,
                        company=item.get("firma", "") or "",
                        location=_place(item),
                        # v6 exposes the home-office flag at search level; v4
                        # only had it on the detail payload.
                        remote=bool(item.get("homeofficemoeglich"))
                        or looks_remote(title),
                        url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}",
                        published_at=publication_start(item),
                        raw=item,
                    )
                )
            except (AttributeError, TypeError) as ex:
                log.warning("arbeitsagentur: skipping malformed item: %s", ex)
        return postings

    async def fetch_details(self, posting: JobPosting) -> JobPosting:
        try:
            resp = await self._client.get(
                detail_url(posting.external_id),
                headers={"X-API-Key": API_KEY},
            )
            resp.raise_for_status()
            detail = resp.json()
        except (httpx.HTTPError, ValueError) as ex:
            # Best-effort: the posting stays usable with search-level data.
            log.info("arbeitsagentur: details unavailable for %s: %s",
                     posting.external_id, ex)
            return posting

        if not isinstance(detail, dict):
            # the endpoint is community-documented and has changed shape before;
            # polling awaits this with no try/except, so one odd payload must
            # not abort the remaining postings of the tick
            log.warning("arbeitsagentur: unexpected detail payload for %s: %s",
                        posting.external_id, type(detail).__name__)
            return posting

        # Field name observed live in July 2026; the older community docs
        # still list "stellenbeschreibung", kept as fallback.
        description = (
            detail.get("stellenangebotsBeschreibung", "")
            or detail.get("stellenbeschreibung", "")
            or ""
        )
        if not isinstance(description, str):
            # guarding the payload's TYPE is not enough — a field of the wrong
            # type reaches extract_email/strip_html and raises out of a call
            # polling awaits unprotected
            log.warning("arbeitsagentur: unexpected description for %s: %s",
                        posting.external_id, type(description).__name__)
            description = ""
        # Some partner listings have no BA-hosted text at all: the full
        # posting lives on the employer's own page (externeURL).
        external_url = _screen_external_url(
            detail.get("externeURL") or detail.get("externeUrl") or "")
        if external_url:
            posting.url = external_url
        if not description and external_url:
            description = await self._fetch_page_text(external_url)

        posting.description = description
        posting.facts = posting_facts(detail)
        posting.contact_email = extract_email(description)
        posting.remote = bool(
            detail.get("homeofficemoeglich")
            or posting.remote
            or looks_remote(posting.title, description)
        )
        if not posting.company:
            # `arbeitgeber` no longer exists in either payload — the old line
            # survived only because of this fallback. `firma` is the sole
            # employer-name source now.
            posting.company = detail.get("firma", "") or ""
        return posting

    async def _fetch_page_text(self, url: str) -> str:
        """Best-effort text of the employer's public posting page.

        The URL is employer-supplied and this runs on the background polling
        path, so the GET goes through the shared netsafe guard: every redirect
        hop is SSRF-screened before its request fires and the body is
        byte-capped. Stripping the result stays off the event loop — the page
        is attacker-shaped and nobody is watching this path. Server-rendered
        career pages yield usable text; JS-heavy ones come back thin — the UI
        lets the user paste the posting manually then.
        """
        page = await netsafe.fetch_text(self._client, url, max_bytes=MAX_PAGE_BYTES)
        if not page:
            # netsafe answers '' for a refused hop, a non-200 and a transport
            # error alike; without this the posting's empty description has no
            # explanation anywhere in the log
            log.info("arbeitsagentur: no usable external page at %s", url)
            return ""
        text = await asyncio.to_thread(strip_html, page)
        return text[:MAX_PAGE_TEXT]
