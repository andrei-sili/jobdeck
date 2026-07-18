"""On-demand resolution of a posting's apply channel.

The classifier (`apply_channel.py`) works on whatever URL we hold, but the
interesting channel (an ATS portal) is usually hidden behind an aggregator
redirect: Jooble stores an ``/away/<id>`` link that 3xx-redirects to the real
posting. This service follows that redirect POLITELY — honest User-Agent, short
timeout, capped redirects, a HEAD-only Location lookup, never a form submission
or a bulk crawl — then classifies the resolved URL.

Only Jooble ``/away/`` links are followed today; Arbeitsagentur already captures
the employer's externeURL at ingestion, and Arbeitnow needs page parsing (a
later slice). A known e-mail short-circuits without any network call, and any
follow failure falls back to classifying the original URL. Resolve on demand
when the user acts on a posting — never in bulk.
"""

import asyncio
import ipaddress
import logging
from urllib.parse import urlsplit

import httpx

from jobdeck import apply_channel, db

log = logging.getLogger(__name__)

_USER_AGENT = "JobDeck/0.1 (+https://github.com/andrei-sili/jobdeck)"
_TIMEOUT = 10.0
_MAX_REDIRECTS = 10


def _is_redirector(url: str) -> bool:
    """True for a Jooble away-link, which 3xx-redirects to the real posting."""
    raw = url if "://" in url else "https://" + url
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    return host.endswith("jooble.org") and parts.path.startswith("/away/")


def _is_public_host(host: str) -> bool:
    """A literal-IP host that is loopback/private/link-local/reserved is NOT
    persisted or navigated to (SSRF defense-in-depth). A hostname is not
    resolved here — full hop-by-hop IP validation is a later slice."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a hostname, not a literal IP
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def _follow(client: httpx.AsyncClient, url: str) -> str:
    """Return the final URL after redirects, or '' on failure / an unsafe host."""
    try:
        resp = await client.head(url, follow_redirects=True)
    except Exception as exc:  # network / timeout / too-many-redirects — non-fatal
        log.info("apply-resolve: could not follow %s: %s", url, exc)
        return ""
    final = str(resp.url)
    if not _is_public_host((urlsplit(final).hostname or "").lower()):
        log.warning("apply-resolve: %s resolved to a non-public host — ignoring", url)
        return ""
    return final


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
    final = url
    if url and _is_redirector(url):
        followed = await _follow(client, url)
        if followed:
            final = followed
    return final, apply_channel.classify(final, email)


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
