import random
import re
import time

import httpx
import pytest

from jobdeck import netsafe
from jobdeck.sources.arbeitnow import ArbeitnowSource
from jobdeck.sources.arbeitsagentur import MAX_PAGE_BYTES, ArbeitsagenturSource
from jobdeck.sources.base import (
    EMAIL_RE,
    SearchQuery,
    SourceUnavailable,
    extract_email,
    looks_remote,
    strip_html,
)
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


@pytest.mark.parametrize("hostile", [
    "javascript:alert(1)",
    "data:text/html,<script>x</script>",
    "file:///etc/passwd",
    "httpevil://intranet.firma.de/x",  # startswith("http") once let this through
])
async def test_arbeitsagentur_hostile_externe_url_is_dropped_at_ingestion(hostile):
    """externeURL is employer-supplied: a non-http(s) value must neither become
    the posting URL nor be fetched — screened at the source, not a later sink."""
    fetched = []

    def handler(request):
        fetched.append(str(request.url))
        if "jobdetails" in str(request.url):
            return httpx.Response(200, json={
                "stellenangebotsBeschreibung": "",
                "externeURL": hostile,
            })
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python"))
    enriched = await source.fetch_details(postings[0])
    assert enriched.url == "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-123"
    assert enriched.description == ""
    assert all("rest.arbeitsagentur.de" in url for url in fetched)


async def test_arbeitsagentur_schemeless_externe_url_gets_https():
    def handler(request):
        url = str(request.url)
        if "jobdetails" in url:
            return httpx.Response(200, json={
                "stellenangebotsBeschreibung": "",
                "externeURL": "www.beispiel.de/jobs/42",
            })
        if request.url.host == "www.beispiel.de":
            return httpx.Response(200, text="<p>Stellenprofil</p>")
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python"))
    enriched = await source.fetch_details(postings[0])
    assert enriched.url == "https://www.beispiel.de/jobs/42"
    assert enriched.description == "Stellenprofil"


async def test_arbeitsagentur_external_page_resolving_private_is_not_fetched(monkeypatch):
    """The SSRF guard runs on the background polling path: an externeURL whose
    host resolves to a private address is never requested."""
    async def resolver(host):
        if host == "intranet.firma.de":
            return ["192.168.1.10"]
        return ["93.184.216.34"]

    monkeypatch.setattr(netsafe, "_system_resolver", resolver)
    fetched = []

    def handler(request):
        fetched.append(request.url.host)
        if "jobdetails" in str(request.url):
            return httpx.Response(200, json={
                "stellenangebotsBeschreibung": "",
                "externeURL": "https://intranet.firma.de/job",
            })
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python"))
    enriched = await source.fetch_details(postings[0])
    assert enriched.description == ""
    assert "intranet.firma.de" not in fetched


async def test_arbeitsagentur_external_redirect_hops_are_screened(monkeypatch):
    """A public externeURL redirecting to a private-resolving host is cut at
    that hop — the redirect target's request never fires."""
    async def resolver(host):
        if host == "intranet.firma.de":
            return ["192.168.1.10"]
        return ["93.184.216.34"]

    monkeypatch.setattr(netsafe, "_system_resolver", resolver)
    fetched = []

    def handler(request):
        fetched.append(request.url.host)
        if "jobdetails" in str(request.url):
            return httpx.Response(200, json={
                "stellenangebotsBeschreibung": "",
                "externeURL": "https://karriere.firma.de/job",
            })
        if request.url.host == "karriere.firma.de":
            return httpx.Response(
                302, headers={"Location": "https://intranet.firma.de/job"})
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python"))
    enriched = await source.fetch_details(postings[0])
    assert enriched.url == "https://karriere.firma.de/job"
    assert enriched.description == ""
    assert "intranet.firma.de" not in fetched


async def test_arbeitsagentur_external_page_is_byte_capped():
    """The adapter must WIRE netsafe's byte cap, not merely truncate the text.

    The page is padding tags (which strip to whitespace) followed by real text
    past byte MAX_PAGE_BYTES, so the marker can only appear if the raw body was
    downloaded beyond the cap — the text cap alone cannot hide it.
    """
    padding = b"<i></i>" * (MAX_PAGE_BYTES // 7 + 1)
    assert len(padding) > MAX_PAGE_BYTES
    pulled = []

    async def content():
        for offset in range(0, len(padding), 64_000):
            pulled.append(offset)
            yield padding[offset:offset + 64_000]
        yield b"BEYONDTHECAP"

    def handler(request):
        url = str(request.url)
        if "jobdetails" in url:
            return httpx.Response(200, json={
                "stellenangebotsBeschreibung": "",
                "externeURL": "https://karriere.beispiel.de/big",
            })
        if request.url.host == "karriere.beispiel.de":
            return httpx.Response(200, content=content())
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python"))
    enriched = await source.fetch_details(postings[0])
    assert "BEYONDTHECAP" not in enriched.description
    # only the tag fragment the cut landed inside survives the strip
    assert len(enriched.description) < 20
    # and the cap bounded the DOWNLOAD, not just the returned text
    assert len(pulled) <= MAX_PAGE_BYTES // 64_000 + 1


async def test_arbeitsagentur_non_str_externe_url_is_ignored():
    """The community-documented BA API has changed shape before: a non-string
    externeURL must not raise out of fetch_details, which polling awaits with
    no try/except — one malformed item would abort the whole profile's poll."""
    def handler(request):
        if "jobdetails" in str(request.url):
            return httpx.Response(200, json={
                "stellenangebotsBeschreibung": "",
                "externeURL": {"url": "https://karriere.beispiel.de/x"},
            })
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python"))
    enriched = await source.fetch_details(postings[0])
    assert enriched.url == "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-123"
    assert enriched.description == ""


@pytest.mark.parametrize("description", [{"text": "x"}, ["a"], 42, True])
async def test_arbeitsagentur_non_str_description_is_not_fatal(description):
    """The payload's type is guarded one level up, but a FIELD of the wrong
    type reaches extract_email/strip_html, and polling awaits fetch_details
    with no try/except — one odd posting would abort the whole tick."""
    def handler(request):
        if "jobdetails" in str(request.url):
            return httpx.Response(200, json={
                "stellenangebotsBeschreibung": description})
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python"))
    enriched = await source.fetch_details(postings[0])
    assert enriched.description == ""
    assert enriched.contact_email == ""


async def test_arbeitsagentur_non_dict_detail_payload_is_not_fatal():
    """Same contract one level up: a JSON array where an object was expected
    must leave the posting usable instead of killing the polling tick."""
    def handler(request):
        if "jobdetails" in str(request.url):
            return httpx.Response(200, json=["unexpected"])
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python"))
    enriched = await source.fetch_details(postings[0])
    assert enriched.external_id == "10001-123"
    assert enriched.description == ""


async def test_arbeitsagentur_whitespace_padded_externe_url_is_normalized():
    def handler(request):
        url = str(request.url)
        if "jobdetails" in url:
            return httpx.Response(200, json={
                "stellenangebotsBeschreibung": "",
                "externeURL": "  www.beispiel.de/jobs/42  ",
            })
        if request.url.host == "www.beispiel.de":
            return httpx.Response(200, text="<p>Stellenprofil</p>")
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python"))
    enriched = await source.fetch_details(postings[0])
    assert enriched.url == "https://www.beispiel.de/jobs/42"


@pytest.mark.parametrize("hostile", [
    "http://192.168.1.1/reboot",
    "https://127.0.0.1:8123/x",
    "http://[::1]/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/x",
])
async def test_arbeitsagentur_private_literal_externe_url_is_not_adopted(hostile):
    """The stored URL is offered to the user's browser, which is on the LAN the
    server-side guard protects us from — so a non-public literal must not become
    the posting URL either."""
    def handler(request):
        if "jobdetails" in str(request.url):
            return httpx.Response(200, json={
                "stellenangebotsBeschreibung": "",
                "externeURL": hostile,
            })
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python"))
    enriched = await source.fetch_details(postings[0])
    assert enriched.url == "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-123"


# --- text extraction: linear rewrites of the two regexes -------------------

def _regex_strip_html(text: str) -> str:
    """The implementation strip_html replaces — the equivalence contract."""
    return re.sub(r"<[^>]+>", " ", text or "").replace("&nbsp;", " ").strip()


def _regex_extract_email(text: str) -> str:
    match = EMAIL_RE.search(text or "")
    return match.group(0).rstrip(".") if match else ""


def test_text_extraction_is_identical_to_the_regexes_it_replaces():
    """Both functions were rewritten for a hostile-input runtime bound, NOT to
    change behaviour: extract_email's output becomes an application recipient.
    Fuzzed over markup, addresses and the non-ASCII characters `\\w` accepts."""
    rng = random.Random(20260804)
    alphabet = list("<>abc /=\"'@.-_+\n\t&;§ßüöÄ²0123456789") + [
        "&nbsp;", "<p>", "</p>", "<a href=\"x>y\">", "<>", "<<", ">>",
        "bewerbung@firma.de", "a.b+c@sub.firma.co.uk", "hr@firma.de.",
    ]
    for _ in range(20_000):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        assert strip_html(s) == _regex_strip_html(s), s
        assert extract_email(s) == _regex_extract_email(s), s


def test_hostile_markup_and_word_runs_are_processed_in_bounded_time():
    """A 400 KB body of '<' (424 bytes gzipped on the wire) took ~52 s of CPU
    through the regex, freezing the whole app from the background poller."""
    started = time.perf_counter()
    assert strip_html("<" * MAX_PAGE_BYTES) == "<" * MAX_PAGE_BYTES
    assert extract_email("a" * MAX_PAGE_BYTES) == ""
    assert time.perf_counter() - started < 2.0


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
