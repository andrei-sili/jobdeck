"""On-demand resolution of a posting's apply channel.

The classifier (`apply_channel.py`) works on whatever URL we hold, but the
interesting channel (an ATS portal) is usually hidden behind an aggregator
redirect: Jooble stores an ``/away/<id>`` link that 3xx-redirects to the real
posting. This service follows that redirect POLITELY — honest User-Agent, short
timeout, capped redirects, a HEAD-only Location lookup, never a form submission
or a bulk crawl — then classifies the resolved URL. When the result looks like
the employer's own site, ONE capped GET inspects the page for ATS vendor
fingerprints (a CNAME'd career domain hides its vendor from the hostname).

Jooble ``/away/`` links are followed; an Arbeitnow posting gets its JOB page
parsed instead (see `_resolve_arbeitnow` — the site's robots.txt allows job
pages but disallows the ``/apply`` route, so that deep-link is only ever
STORED for the human to open, never fetched); Arbeitsagentur already captures
the employer's externeURL at ingestion. A known e-mail short-circuits without
any network call, and any failure falls back to classifying the original URL.
Resolve on demand when the user acts on a posting — never in bulk.
"""

import asyncio
import logging
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from jobdeck import apply_channel, db, netsafe

log = logging.getLogger(__name__)

_USER_AGENT = "JobDeck/0.1 (+https://github.com/andrei-sili/jobdeck)"
_TIMEOUT = 10.0
_MAX_REDIRECTS = 10
_MAX_BYTES = 400_000


def _is_redirector(url: str) -> bool:
    """True for a Jooble away-link, which 3xx-redirects to the real posting."""
    raw = url if "://" in url else "https://" + url
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    return host.endswith("jooble.org") and parts.path.startswith("/away/")


def _is_arbeitnow_job(url: str) -> bool:
    """True for an Arbeitnow job page (its feed URLs all point there)."""
    raw = url if "://" in url else "https://" + url
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    return (host == "arbeitnow.com" or host.endswith(".arbeitnow.com")) \
        and parts.path.startswith("/jobs/")


class _ArbeitnowPage(HTMLParser):
    """Signals an Arbeitnow job page carries in raw server-rendered HTML:
    anchor hrefs (one is the ``…/apply`` deep-link) and form ids (the
    JOIN-powered quick-apply variant embeds ``form_job_application``)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.form_ids: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.hrefs.append(values["href"])
        elif tag == "form" and values.get("id"):
            self.form_ids.append(values["id"])


def _arbeitnow_apply_href(hrefs: list[str]) -> str:
    """The page's own ``…/apply`` deep-link, '' when the layout changed."""
    for href in hrefs:
        parts = urlsplit(href)
        host = (parts.hostname or "").lower()
        if (host == "www.arbeitnow.com" or host == "arbeitnow.com") \
                and parts.path.startswith("/jobs/") \
                and parts.path.rstrip("/").endswith("/apply"):
            return href
    return ""


async def _resolve_arbeitnow(
    client: httpx.AsyncClient, url: str
) -> tuple[str, apply_channel.ApplyChannel] | None:
    """Resolve an Arbeitnow posting from its JOB page (robots-allowed).

    The board hosts the apply control itself: an ``…/apply`` route that
    302-redirects to the real destination. robots.txt DISALLOWS that route to
    bots, so this never fetches it — the deep-link is stored for the HUMAN to
    open, and the click lands them on the real ATS/company form. The
    JOIN-powered quick-apply variant is recognizable from the embedded form
    and labeled as the JOIN ATS. None on any failure -> caller falls back."""
    page = await netsafe.fetch_text(
        client, url, max_bytes=_MAX_BYTES, max_redirects=_MAX_REDIRECTS)
    if not page:
        return None
    parser = _ArbeitnowPage()
    try:
        parser.feed(page)
        parser.close()
    except Exception:  # hostile/odd HTML — no signal, keep the fallback path
        return None
    apply_href = _arbeitnow_apply_href(parser.hrefs)
    if "form_job_application" in parser.form_ids:
        return apply_href or url, apply_channel.ApplyChannel(
            apply_channel.CHANNEL_ATS, "JOIN")
    if apply_href:
        return apply_href, apply_channel.ApplyChannel(
            apply_channel.CHANNEL_BOARD, "Arbeitnow")
    return None


async def _follow(client: httpx.AsyncClient, url: str) -> str:
    """Return the final URL after redirects, or '' on failure or an unsafe hop.

    HEAD-only manual walk: every hop's scheme and host pass the shared SSRF
    guard (netsafe — literal screen + all-resolved-IPs-public) BEFORE its
    request fires, so an intermediate redirect can no more touch a private
    network than the final one."""
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        if not await netsafe.url_is_safe(current):
            log.warning("apply-resolve: %s reached an unsafe hop — ignoring", url)
            return ""
        try:
            resp = await client.send(client.build_request("HEAD", current))
        except Exception as exc:  # network / timeout — non-fatal
            log.info("apply-resolve: could not follow %s: %s", url, exc)
            return ""
        if resp.next_request is None:
            return current
        current = str(resp.next_request.url)
    log.info("apply-resolve: %s redirected more than %d times — giving up",
             url, _MAX_REDIRECTS)
    return ""


async def _inspect_page(
    client: httpx.AsyncClient, url: str
) -> apply_channel.ApplyChannel | None:
    """One polite capped GET of a company-site page to look for ATS vendor
    fingerprints (a CNAME'd career domain hides its vendor from the hostname).
    Any failure or non-match keeps the existing classification."""
    page = await netsafe.fetch_text(
        client, url, max_bytes=_MAX_BYTES, max_redirects=_MAX_REDIRECTS)
    if not page:
        return None
    return apply_channel.detect_ats_from_page(page)


async def resolve(
    job, client: httpx.AsyncClient
) -> tuple[str, apply_channel.ApplyChannel]:
    """Resolve the final apply URL for a posting and classify it. Does not touch
    the DB — the caller persists the result. A known e-mail wins immediately
    (auto-sendable), skipping the network."""
    url = (job["url"] or "").strip()
    email = (job["contact_email"] or "").strip()
    if email:
        return url, apply_channel.classify(url, email)
    if url and _is_arbeitnow_job(url):
        resolved = await _resolve_arbeitnow(client, url)
        if resolved is not None:
            return resolved
    final = url
    if url and _is_redirector(url):
        followed = await _follow(client, url)
        if followed:
            final = followed
    ch = apply_channel.classify(final, email)
    if ch.channel == apply_channel.CHANNEL_COMPANY_SITE:
        detected = await _inspect_page(client, final)
        if detected is not None:
            ch = detected
    return final, ch


def _load_job(job_id: int):
    with db.db() as con:
        return db.get_job(con, job_id)


def _store(job_id: int, ch: apply_channel.ApplyChannel, apply_url: str) -> None:
    with db.db() as con:
        db.set_apply_channel(con, job_id, ch.channel, ch.vendor, apply_url)


async def resolve_and_store(job_id: int) -> dict:
    """Resolve one posting's apply channel and persist it. On-demand only."""
    job = await asyncio.to_thread(_load_job, job_id)
    if job is None:
        return {"ok": False, "channel": apply_channel.CHANNEL_UNKNOWN,
                "vendor": "", "apply_url": ""}
    async with httpx.AsyncClient(
        headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT,
        max_redirects=_MAX_REDIRECTS,
    ) as client:
        final, ch = await resolve(job, client)
    await asyncio.to_thread(_store, job_id, ch, final)
    return {"ok": True, "channel": ch.channel, "vendor": ch.vendor,
            "apply_url": final}
