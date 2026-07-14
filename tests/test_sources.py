import httpx
import pytest

from jobdeck.sources.arbeitnow import ArbeitnowSource
from jobdeck.sources.arbeitsagentur import ArbeitsagenturSource
from jobdeck.sources.base import SearchQuery, SourceUnavailable, extract_email, looks_remote
from jobdeck.sources.jooble import JoobleSource

BA_SEARCH = {
    "stellenangebote": [
        {
            "refnr": "10001-123",
            "titel": "Python Entwickler (m/w/d)",
            "arbeitgeber": "Eurogard GmbH",
            "arbeitsort": {"ort": "Herzogenrath"},
            "aktuelleVeroeffentlichungsdatum": "2026-07-10",
        },
        {"kaputt": True},  # malformed item: must be skipped, not fatal
        {
            "refnr": "10001-456",
            "beruf": "Fachinformatiker",
            "arbeitgeber": "ncsolution GmbH",
            "arbeitsort": None,
        },
    ]
}

BA_DETAILS = {
    # real key observed live (July 2026)
    "stellenangebotsBeschreibung": "Wir suchen... Bewerbung an hr@eurogard.de. Remote möglich.",
    "arbeitgeber": "Eurogard GmbH",
}

BA_DETAILS_LEGACY_KEY = {
    # older community-documented key, kept working as fallback
    "stellenbeschreibung": "Legacy-Feld. Kontakt: alt@firma.de",
}

JOOBLE = {
    "totalCount": 1,
    "jobs": [
        {
            "id": 987654,
            "title": "Backend Developer Python",
            "company": "Beispiel AG",
            "location": "Berlin",
            "snippet": "<b>Python</b> und FastAPI. Kontakt: jobs@beispiel.de",
            "link": "https://de.jooble.org/job/987654",
            "updated": "2026-07-12",
        }
    ],
}

ARBEITNOW = {
    "data": [
        {
            "slug": "python-dev-hamburg",
            "title": "Python Developer",
            "company_name": "Startup GmbH",
            "location": "Hamburg",
            "remote": True,
            "url": "https://arbeitnow.com/jobs/python-dev-hamburg",
            "description": "<p>Django, PostgreSQL</p>",
            "tags": ["python", "django"],
            "created_at": 1780000000,
        },
        {
            "slug": "java-dev",
            "title": "Java Developer",
            "company_name": "Enterprise AG",
            "location": "München",
            "remote": False,
            "url": "https://arbeitnow.com/jobs/java-dev",
            "description": "<p>Java only</p>",
            "tags": ["java"],
        },
    ]
}


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_arbeitsagentur_search_defensive():
    def handler(request):
        assert request.headers["X-API-Key"] == "jobboerse-jobsuche"
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python", location="Aachen",
                                               radius_km=50))
    assert len(postings) == 2  # malformed item skipped
    assert postings[0].external_id == "10001-123"
    assert postings[0].company == "Eurogard GmbH"
    assert "jobdetail/10001-123" in postings[0].url
    assert postings[1].title == "Fachinformatiker"  # beruf fallback, arbeitsort None


async def test_arbeitsagentur_details_enrich():
    def handler(request):
        if "jobdetails" in str(request.url):
            return httpx.Response(200, json=BA_DETAILS)
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python"))
    enriched = await source.fetch_details(postings[0])
    assert enriched.contact_email == "hr@eurogard.de"
    assert enriched.remote is True  # "Remote möglich" in description


async def test_arbeitsagentur_details_legacy_field_fallback():
    def handler(request):
        if "jobdetails" in str(request.url):
            return httpx.Response(200, json=BA_DETAILS_LEGACY_KEY)
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python"))
    enriched = await source.fetch_details(postings[0])
    assert enriched.contact_email == "alt@firma.de"


async def test_arbeitsagentur_partner_posting_fetches_external_page():
    """IT postings are mostly partner listings: empty stellenbeschreibung,
    full text on the employer's page behind externeURL."""

    def handler(request):
        url = str(request.url)
        if "jobdetails" in url:
            return httpx.Response(200, json={
                "stellenbeschreibung": "",
                "externeURL": "https://karriere.beispiel.de/python-dev",
                "homeofficemoeglich": True,
                "firma": "Beispiel AG",
            })
        if "karriere.beispiel.de" in url:
            return httpx.Response(200, text=(
                "<html><body><h1>Python Entwickler</h1>"
                "<p>Django und FastAPI. Bewerbung an career@beispiel.de.</p>"
                "</body></html>"))
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python"))
    enriched = await source.fetch_details(postings[0])
    assert enriched.url == "https://karriere.beispiel.de/python-dev"
    assert "Django und FastAPI" in enriched.description
    assert enriched.contact_email == "career@beispiel.de"
    assert enriched.remote is True  # homeofficemoeglich flag


async def test_arbeitsagentur_details_failure_keeps_posting():
    def handler(request):
        if "jobdetails" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python"))
    enriched = await source.fetch_details(postings[0])
    assert enriched.external_id == "10001-123" and enriched.description == ""


async def test_arbeitsagentur_search_failure_raises_unavailable():
    source = ArbeitsagenturSource(make_client(lambda r: httpx.Response(503)))
    with pytest.raises(SourceUnavailable):
        await source.search(SearchQuery(keywords="Python"))


async def test_jooble_search(monkeypatch):
    monkeypatch.setenv("JOOBLE_API_KEY", "test-key")

    def handler(request):
        assert str(request.url).endswith("/api/test-key")
        return httpx.Response(200, json=JOOBLE)

    source = JoobleSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="python"))
    assert len(postings) == 1
    posting = postings[0]
    assert posting.external_id == "987654"
    assert posting.contact_email == "jobs@beispiel.de"
    assert "<b>" not in posting.description  # HTML stripped


async def test_jooble_without_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("JOOBLE_API_KEY", raising=False)
    source = JoobleSource(make_client(lambda r: httpx.Response(200, json={})))
    with pytest.raises(SourceUnavailable):
        await source.search(SearchQuery(keywords="python"))


async def test_arbeitnow_filters_by_keywords():
    def handler(request):
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=ARBEITNOW if page == 1 else {"data": []})

    source = ArbeitnowSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="python"))
    assert [p.external_id for p in postings] == ["python-dev-hamburg"]
    assert postings[0].remote is True


async def test_arbeitnow_location_filter_allows_remote():
    def handler(request):
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json=ARBEITNOW if page == 1 else {"data": []})

    source = ArbeitnowSource(make_client(handler))
    # remote job in Hamburg matches an Aachen-located profile because remote=True
    postings = await source.search(SearchQuery(keywords="python", location="Aachen"))
    assert [p.external_id for p in postings] == ["python-dev-hamburg"]


def test_extract_email_and_remote_markers():
    assert extract_email("Bewerbung an hr@firma-x.de bitte") == "hr@firma-x.de"
    assert extract_email("kein kontakt") == ""
    assert looks_remote("Python Dev (Home Office)")
    assert not looks_remote("Python Dev vor Ort")
