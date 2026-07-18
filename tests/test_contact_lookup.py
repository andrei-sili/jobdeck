"""Tests for the deterministic on-demand company-e-mail lookup."""

import httpx

from jobdeck.services import contact_lookup as cl


def _job(url):
    return {"apply_url": url, "url": url, "contact_email": ""}


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_employer_host_only_for_a_company_site():
    assert cl._employer_host(_job("https://firma.de/karriere/1")) == "firma.de"
    assert cl._employer_host(_job("https://de.jooble.org/desc/1")) == ""      # board
    assert cl._employer_host(_job("https://acme.jobs.personio.de/9")) == ""   # ATS
    assert cl._employer_host(_job("http://127.0.0.1/x")) == ""                # private


async def test_finds_a_dedicated_address_on_the_impressum():
    def handler(request):
        if str(request.url).endswith("/impressum"):
            return httpx.Response(
                200, text="Kontakt: info@firma.de · Bewerbungen: bewerbung@firma.de")
        return httpx.Response(404)

    async with _client(handler) as client:
        r = await cl.lookup(_job("https://firma.de/karriere/stelle-1"), client)
    assert r["email"] == "bewerbung@firma.de" and r["dedicated"] is True
    assert r["source_url"] == "https://firma.de/impressum"


async def test_only_a_generic_address_is_flagged_not_dedicated():
    def handler(request):
        if "/impressum" in str(request.url):
            return httpx.Response(200, text="Impressum — E-Mail: info@firma.de")
        return httpx.Response(404)

    async with _client(handler) as client:
        r = await cl.lookup(_job("https://firma.de/jobs/2"), client)
    assert r["email"] == "info@firma.de"
    assert r["generic"] is True and r["dedicated"] is False


async def test_a_board_posting_is_not_looked_up():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="bewerbung@firma.de")

    async with _client(handler) as client:
        r = await cl.lookup(_job("https://de.jooble.org/desc/1"), client)
    assert calls == []  # unknown employer domain -> no fetch
    assert r["email"] == ""


async def test_a_redirect_to_a_private_host_reads_nothing():
    def handler(request):
        u = str(request.url)
        if "127.0.0.1" in u:  # the private target body is never read
            return httpx.Response(200, text="bewerbung@firma.de")
        if u.endswith("/impressum"):
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:9/x"})
        return httpx.Response(404)

    async with _client(handler) as client:
        r = await cl.lookup(_job("https://firma.de/x"), client)
    assert r["email"] == ""  # SSRF guard: the private-host page is dropped
