"""Tests for the on-demand apply-channel resolver (redirect-follow + store)."""

import asyncio

import httpx

from jobdeck import apply_channel as ac
from jobdeck import db
from jobdeck.services import apply_resolve


def _job(url, email=""):
    return {"url": url, "contact_email": email}


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_follows_a_jooble_away_link_to_the_real_ats():
    def handler(request):
        if "/away/" in str(request.url):
            return httpx.Response(
                302, headers={"Location": "https://join.com/companies/acme/1-dev"})
        return httpx.Response(200)

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(
            _job("https://de.jooble.org/away/123"), client)
    assert final == "https://join.com/companies/acme/1-dev"
    assert ch.channel == ac.CHANNEL_ATS and ch.vendor == "JOIN"


async def test_a_jooble_desc_page_is_not_followed():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200)

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(
            _job("https://de.jooble.org/desc/9"), client)
    assert calls == []  # a non-redirector makes no network call
    assert ch.channel == ac.CHANNEL_BOARD and ch.vendor == "Jooble"
    assert final == "https://de.jooble.org/desc/9"


async def test_a_known_email_short_circuits_without_network():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200)

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(
            _job("https://de.jooble.org/away/1", "jobs@acme.de"), client)
    assert calls == []  # a direct e-mail is decisive; no redirect follow
    assert ch.channel == ac.CHANNEL_DIRECT_EMAIL
    assert final == "https://de.jooble.org/away/1"


async def test_a_redirect_to_a_private_host_is_ignored():
    # a poisoned redirect chain to an internal address must not be persisted or
    # navigated to — the resolver drops it and falls back to the original URL
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if "/away/" in str(request.url):
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8080/x"})
        return httpx.Response(200)

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(
            _job("https://de.jooble.org/away/6"), client)
    assert final == "https://de.jooble.org/away/6"  # fell back, did not store 127.0.0.1
    assert ch.channel == ac.CHANNEL_BOARD and ch.vendor == "Jooble"
    assert calls == ["de.jooble.org"]  # the private hop was never even requested


async def test_a_hop_whose_hostname_resolves_private_is_rejected(monkeypatch):
    # DNS-level SSRF: internal.firma.de is a HOSTNAME resolving to a private
    # address — the guard must resolve it and refuse before the request fires
    from jobdeck import netsafe

    async def fake_resolver(host):
        return ["192.168.1.10"] if host == "internal.firma.de" else ["93.184.216.34"]

    monkeypatch.setattr(netsafe, "_system_resolver", fake_resolver)
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if "/away/" in str(request.url):
            return httpx.Response(
                302, headers={"Location": "https://internal.firma.de/admin"})
        return httpx.Response(200)

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(
            _job("https://de.jooble.org/away/7"), client)
    assert final == "https://de.jooble.org/away/7"
    assert calls == ["de.jooble.org"]  # never requested the private-resolving host


async def test_a_mixed_public_private_dns_answer_is_rejected(monkeypatch):
    # one private record in the answer poisons the whole host (the OS may
    # connect to any of them) — fail closed
    from jobdeck import netsafe

    async def fake_resolver(host):
        if host == "evil.example.com":
            return ["93.184.216.34", "10.0.0.5"]
        return ["93.184.216.34"]

    monkeypatch.setattr(netsafe, "_system_resolver", fake_resolver)

    def handler(request):
        if "/away/" in str(request.url):
            return httpx.Response(
                302, headers={"Location": "https://evil.example.com/jobs"})
        return httpx.Response(200)

    async with _client(handler) as client:
        final, _ = await apply_resolve.resolve(
            _job("https://de.jooble.org/away/8"), client)
    assert final == "https://de.jooble.org/away/8"


async def test_an_unresolvable_host_falls_back_to_the_original_url(monkeypatch):
    import socket

    from jobdeck import netsafe

    async def fake_resolver(host):
        if host == "gone.example.com":
            raise socket.gaierror("NXDOMAIN")
        return ["93.184.216.34"]

    monkeypatch.setattr(netsafe, "_system_resolver", fake_resolver)

    def handler(request):
        if "/away/" in str(request.url):
            return httpx.Response(
                302, headers={"Location": "https://gone.example.com/x"})
        return httpx.Response(200)

    async with _client(handler) as client:
        final, _ = await apply_resolve.resolve(
            _job("https://de.jooble.org/away/9"), client)
    assert final == "https://de.jooble.org/away/9"


async def test_a_company_site_page_with_vendor_markers_upgrades_to_ats():
    # jobs.hoermann.de is a rexx portal behind a CNAME — the landing host says
    # company_site, but the page's form action betrays the vendor
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, text=(
            '<form action="https://hoermann.rexx-systems.com/apply/1"></form>'))

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(
            _job("https://jobs.hoermann.de/stelle-42"), client)
    assert final == "https://jobs.hoermann.de/stelle-42"  # URL stays the page
    assert ch.channel == ac.CHANNEL_ATS and ch.vendor == "rexx systems"


async def test_a_company_site_page_without_markers_stays_company_site():
    def handler(request):
        return httpx.Response(200, text="<h1>Karriere</h1><p>Mail uns.</p>")

    async with _client(handler) as client:
        _, ch = await apply_resolve.resolve(
            _job("https://firma.de/karriere/dev"), client)
    assert ch.channel == ac.CHANNEL_COMPANY_SITE and ch.vendor == ""


async def test_a_failing_company_site_fetch_keeps_the_classification():
    def handler(request):
        raise httpx.ConnectError("down")

    async with _client(handler) as client:
        _, ch = await apply_resolve.resolve(
            _job("https://firma.de/karriere/dev"), client)
    assert ch.channel == ac.CHANNEL_COMPANY_SITE


async def test_an_ats_or_board_landing_is_never_page_fetched():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="x")

    async with _client(handler) as client:
        await apply_resolve.resolve(
            _job("https://acme.jobs.personio.de/job/1"), client)
        await apply_resolve.resolve(_job("https://de.jooble.org/desc/2"), client)
    assert calls == []  # the classification was decisive without any network


async def test_a_redirect_loop_gives_up_and_falls_back():
    def handler(request):  # every hop redirects forever
        return httpx.Response(302, headers={"Location": str(request.url)})

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(
            _job("https://de.jooble.org/away/10"), client)
    assert final == "https://de.jooble.org/away/10"
    assert ch.channel == ac.CHANNEL_BOARD and ch.vendor == "Jooble"


async def test_a_follow_failure_falls_back_to_the_original_url():
    def handler(request):
        raise httpx.ConnectError("boom")

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(
            _job("https://de.jooble.org/away/5"), client)
    assert final == "https://de.jooble.org/away/5"
    assert ch.channel == ac.CHANNEL_BOARD and ch.vendor == "Jooble"


def test_resolve_and_store_persists_the_channel(con):
    # a non-redirector posting (no network) end-to-end through the real tmp DB
    job_id = db.insert_job_if_new(con, {
        "source": "jooble", "external_id": "j-desc-1",
        "title": "Dev", "company": "Acme", "description": "desc",
        "url": "https://de.jooble.org/desc/42",
    })
    con.commit()
    res = asyncio.run(apply_resolve.resolve_and_store(job_id))
    assert res["ok"]
    assert res["channel"] == ac.CHANNEL_BOARD and res["vendor"] == "Jooble"
    row = db.get_job(con, job_id)
    assert row["apply_channel"] == ac.CHANNEL_BOARD
    assert row["ats_vendor"] == "Jooble"
    assert row["apply_url"] == "https://de.jooble.org/desc/42"
