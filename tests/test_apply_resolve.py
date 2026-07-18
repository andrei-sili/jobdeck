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
    def handler(request):
        if "/away/" in str(request.url):
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:8080/x"})
        return httpx.Response(200)

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(
            _job("https://de.jooble.org/away/6"), client)
    assert final == "https://de.jooble.org/away/6"  # fell back, did not store 127.0.0.1
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
