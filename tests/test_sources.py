import random
import re
import time

import httpx
import pytest

from jobdeck import netsafe
from jobdeck.sources import arbeitsagentur
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

# v6 search shape, observed live 2026-08-05: the envelope key is
# `ergebnisliste`, and none of the v4 item names survive.
BA_SEARCH = {
    "ergebnisliste": [
        {
            "referenznummer": "10001-123",
            "stellenangebotsTitel": "Python Entwickler (m/w/d)",
            "hauptberuf": "Softwareentwickler/in",  # BERUFENET label, NOT a title
            "firma": "Eurogard GmbH",
            "stellenlokationen": [
                {"adresse": {"ort": "Herzogenrath", "land": "DEUTSCHLAND"}}
            ],
            # the API states both dates: this ad first appeared long ago and
            # was re-published, so only the current period says how old it is
            "datumErsteVeroeffentlichung": "2025-01-28",
            "veroeffentlichungszeitraum": {"von": "2026-07-10"},
        },
        {"kaputt": True},  # malformed item: must be skipped, not fatal
        {
            "referenznummer": "10001-456",
            "stellenangebotsTitel": "Fachinformatiker Anwendungsentwicklung",
            "firma": "ncsolution GmbH",
            "stellenlokationen": None,
        },
    ]
}

BA_DETAILS = {
    # real key observed live (July 2026, still current on the v4 detail route)
    "stellenangebotsBeschreibung": "Wir suchen... Bewerbung an hr@eurogard.de. Remote möglich.",
    "firma": "Eurogard GmbH",
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
    assert postings[1].title == "Fachinformatiker Anwendungsentwicklung"
    assert postings[1].location == ""  # stellenlokationen None must not raise
    # the employer's own title wins over the standardised profession label
    assert postings[0].title == "Python Entwickler (m/w/d)"
    assert postings[0].location == "Herzogenrath"  # German: no country suffix
    # the CURRENT publication period, not the first-ever appearance: employers
    # re-publish constantly and freshness must judge the ad in front of him
    assert postings[0].published_at == "2026-07-10"
    assert postings[1].published_at == ""  # neither date stated: unknown, not today


@pytest.mark.parametrize("payload, expected", [
    ({"veroeffentlichungszeitraum": {"von": "2026-07-08"},
      "datumErsteVeroeffentlichung": "2025-01-28"}, "2026-07-08"),
    ({"datumErsteVeroeffentlichung": "2025-01-28"}, "2025-01-28"),
    ({"veroeffentlichungszeitraum": {"von": "  2026-07-08  "}}, "2026-07-08"),
    ({"veroeffentlichungszeitraum": {"von": ""},
      "datumErsteVeroeffentlichung": "2025-01-28"}, "2025-01-28"),
    ({"veroeffentlichungszeitraum": "kaputt"}, ""),
    ({"veroeffentlichungszeitraum": {"von": 20260708}}, ""),  # wrong type
    ({}, ""),
    ("not a payload", ""),
    (None, ""),
])
def test_publication_start_prefers_the_current_period(payload, expected):
    from jobdeck.sources.arbeitsagentur import publication_start
    assert publication_start(payload) == expected


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


async def test_arbeitsagentur_never_stores_the_profession_label_as_the_title():
    """18 stored postings carried the generic BERUFENET label
    "Fachinformatiker/in - Anwendungsentwicklung" as their title instead of
    the employer's own, losing the one line that says what the job is — and
    feeding that loss straight into the match score. v4's `beruf` fallback is
    what did it, so there is deliberately no fallback now."""
    payload = {"ergebnisliste": [
        {
            "referenznummer": "1-a",
            "stellenangebotsTitel": "Junior Backend Developer Python (m/w/d)",
            "hauptberuf": "Fachinformatiker/in - Anwendungsentwicklung",
            "alleBerufe": ["Fachinformatiker/in - Anwendungsentwicklung"],
            "firma": "Echte Firma GmbH",
        },
        {   # no title of its own: skipped rather than stored mislabelled
            "referenznummer": "1-b",
            "hauptberuf": "Fachinformatiker/in - Anwendungsentwicklung",
            "alleBerufe": ["Fachinformatiker/in - Anwendungsentwicklung"],
            "firma": "Andere Firma GmbH",
        },
    ]}
    source = ArbeitsagenturSource(
        make_client(lambda r: httpx.Response(200, json=payload)))
    postings = await source.search(SearchQuery(keywords="Python"))
    assert [p.title for p in postings] == ["Junior Backend Developer Python (m/w/d)"]


async def test_arbeitsagentur_marks_a_posting_outside_germany():
    """Austrian AMS listings leaked into a nationwide GERMAN search looking
    exactly like domestic ones — same rows as the generic-title bug. They are
    kept, but the country travels with the location so nobody reads "Wien" as
    a German city."""
    payload = {"ergebnisliste": [
        {"referenznummer": "at-1", "stellenangebotsTitel": "Entwickler",
         "firma": "AT GmbH",
         "stellenlokationen": [{"adresse": {"ort": "Wien", "land": "OESTERREICH"}}]},
        {"referenznummer": "de-1", "stellenangebotsTitel": "Entwickler",
         "firma": "DE GmbH",
         "stellenlokationen": [{"adresse": {"ort": "Köln", "land": "DEUTSCHLAND"}}]},
    ]}
    source = ArbeitsagenturSource(
        make_client(lambda r: httpx.Response(200, json=payload)))
    postings = await source.search(SearchQuery(keywords="Entwickler"))
    assert [p.location for p in postings] == ["Wien, OESTERREICH", "Köln"]


async def test_arbeitsagentur_search_uses_v6_and_details_stay_on_v4():
    """The search route moved to v6; the DETAIL route did not and has no v6 —
    every v6/v5/v2/v1 jobdetails path answers 403 as an unregistered route.
    Pinning both stops a well-meant "upgrade everything" from breaking detail."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "jobdetails" in str(request.url):
            return httpx.Response(200, json=BA_DETAILS)
        return httpx.Response(200, json=BA_SEARCH)

    source = ArbeitsagenturSource(make_client(handler))
    postings = await source.search(SearchQuery(keywords="Python"))
    await source.fetch_details(postings[0])
    assert "/pc/v6/jobs" in seen[0]
    assert "/pc/v4/jobdetails/" in seen[1]


async def test_arbeitsagentur_zero_hits_omit_the_result_key():
    """v6 drops `ergebnisliste` entirely on a no-hit query instead of sending
    an empty list, so a plain .get(key, []) would still work but .get(key) or []
    is what survives the None the API actually sends for some shapes."""
    source = ArbeitsagenturSource(make_client(
        lambda r: httpx.Response(200, json={"maxErgebnisse": 0, "page": 1, "size": 5})))
    assert await source.search(SearchQuery(keywords="nichts")) == []


# ---------------------------------------------------------------------------
# Facts the detail payload states and the app used to throw away
# ---------------------------------------------------------------------------
def test_posting_facts_reads_the_street_level_work_address():
    facts = arbeitsagentur.posting_facts({
        "stellenlokationen": [{"adresse": {
            "strasse": "Musterstraße", "hausnummer": "26",
            "plz": "70178", "ort": "Stuttgart", "land": "DEUTSCHLAND"}}],
    })
    assert facts["work_strasse"] == "Musterstraße 26"
    assert facts["work_plz_ort"] == "70178 Stuttgart"


def test_posting_facts_survives_a_half_stated_address():
    facts = arbeitsagentur.posting_facts({
        "stellenlokationen": [{"adresse": {"ort": "Aachen"}}]})
    assert facts["work_strasse"] == ""
    assert facts["work_plz_ort"] == "Aachen"


def test_posting_facts_reads_the_pay_range_and_what_it_means():
    """`verguetungsangabe` is the period; `artDerVerguetung` only says whether
    the figure is a range or a fixed sum, which the two numbers already show."""
    facts = arbeitsagentur.posting_facts({
        "gehaltsspanneVon": 37000, "gehaltsspanneBis": "47000.0",
        "artDerVerguetung": "GEHALTSSPANNE",
        "verguetungsangabe": "JAHRESGEHALT"})
    assert (facts["salary_from"], facts["salary_to"]) == ("37000", "47000")
    assert facts["salary_period"] == "JAHRESGEHALT"


def test_an_hourly_wage_keeps_its_cents():
    """The same field carries 55000.0 for a year and 30.32 for an hour —
    measured live on the real API. Rounded to euros the offer changes."""
    facts = arbeitsagentur.posting_facts({
        "gehaltsspanneVon": 30.32, "gehaltsspanneBis": 33.69,
        "verguetungsangabe": "STUNDENLOHN"})
    assert (facts["salary_from"], facts["salary_to"]) == ("30.32", "33.69")
    assert facts["salary_period"] == "STUNDENLOHN"


def test_a_pay_figure_that_is_not_a_number_is_not_invented():
    facts = arbeitsagentur.posting_facts({
        "gehaltsspanneVon": "nach Vereinbarung", "gehaltsspanneBis": None})
    assert (facts["salary_from"], facts["salary_to"]) == ("", "")


def test_a_non_finite_pay_figure_is_refused_at_the_door():
    """json.loads — which httpx's .json() uses — accepts the non-standard
    `Infinity` and `NaN` literals, and "1e999" parses to inf. Neither
    `inf <= 0` nor `nan <= 0` is true, so the positivity bound is not what
    stops them; stored, they would later raise out of the inbox's render."""
    facts = arbeitsagentur.posting_facts({
        "gehaltsspanneVon": float("inf"), "gehaltsspanneBis": float("nan")})
    assert (facts["salary_from"], facts["salary_to"]) == ("", "")
    assert arbeitsagentur.posting_facts(
        {"gehaltsspanneVon": "1e999"})["salary_from"] == ""


def test_posting_facts_flags_arbeitnehmerueberlassung():
    assert arbeitsagentur.posting_facts(
        {"istArbeitnehmerUeberlassung": True})["temp_agency"] == 1
    assert arbeitsagentur.posting_facts({})["temp_agency"] == 0


def test_posting_facts_tolerates_a_payload_of_the_wrong_shape():
    """The endpoint is community-documented and has changed shape before; this
    runs on the background polling path and on every liveness probe."""
    assert arbeitsagentur.posting_facts("not a payload") == {}
    assert arbeitsagentur.posting_facts(
        {"stellenlokationen": "wrong"})["work_plz_ort"] == ""


def test_posting_facts_speak_only_the_jobs_table_vocabulary():
    """The writer refuses a key nobody stores, so a source cannot lose data in
    silence — this pins the two halves of that contract together."""
    from jobdeck import db as db_module
    facts = arbeitsagentur.posting_facts({"gehaltsspanneVon": 1})
    assert set(facts) <= set(db_module.JOB_FACT_COLUMNS)
