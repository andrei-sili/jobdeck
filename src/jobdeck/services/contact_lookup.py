"""On-demand lookup of a company's application e-mail.

Deterministic path: for a posting whose apply channel is the employer's OWN site
(we know the employer domain), fetch the Impressum / Kontakt / Karriere page and
pick a domain-verified application address (`contact_resolve`). An LLM web-search
fallback for discovering the domain of board/ATS postings is added behind a
per-install toggle in a later commit.

This PROPOSES an address — it never adopts it: the caller/UI shows the proposal
and the human confirms before it can become a send recipient. Fetches politely
(honest User-Agent, timeout, capped body), rejects a private/link-local host
(SSRF), and treats every fetched page as untrusted data.
"""

import asyncio
import ipaddress
import logging
from urllib.parse import urlsplit

import httpx

from jobdeck import apply_channel, contact_resolve, db

log = logging.getLogger(__name__)

_USER_AGENT = "JobDeck/0.1 (+https://github.com/andrei-sili/jobdeck)"
_TIMEOUT = 12.0
_MAX_BYTES = 400_000
_MAX_REDIRECTS = 10
# Pages that carry the §5 DDG contact e-mail, best first.
_PATHS = ("/impressum", "/kontakt", "/karriere", "/impressum/", "/")

_EMPTY = {"email": "", "dedicated": False, "generic": False, "source_url": ""}


def _public_host(url: str) -> str:
    """Host of a URL, or '' when it is a literal private/loopback/link-local IP."""
    host = (urlsplit(url if "://" in url else "//" + url).hostname or "").lower()
    if not host:
        return ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host  # a hostname
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        return ""
    return host


def _employer_host(job) -> str:
    """The employer's own host, but only when the apply channel is their own
    site — never a board or an ATS host (which are not the employer's domain)."""
    url = job["apply_url"] or job["url"] or ""
    host = _public_host(url)
    if not host:
        return ""
    if apply_channel.classify(url, "").channel == apply_channel.CHANNEL_COMPANY_SITE:
        return host
    return ""


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str:
    """GET a page body (untrusted), or '' on failure / a non-public final host."""
    try:
        resp = await client.get(url, follow_redirects=True)
    except Exception as exc:  # network / timeout / too-many-redirects — non-fatal
        log.info("contact-lookup: fetch %s failed: %s", url, exc)
        return ""
    if resp.status_code != 200 or not _public_host(str(resp.url)):
        return ""
    return resp.text[:_MAX_BYTES]


async def _lookup_on_host(client: httpx.AsyncClient, host: str) -> dict:
    """Try the contact pages on one host; return the first dedicated address,
    else the first (generic/named) one found."""
    fallback = dict(_EMPTY)
    for path in _PATHS:
        url = f"https://{host}{path}"
        r = contact_resolve.resolve_email(await _fetch_text(client, url), host)
        if r["dedicated"]:
            return {**r, "source_url": url}
        if r["email"] and not fallback["email"]:
            fallback = {**r, "source_url": url}
    return fallback


async def lookup(job, client: httpx.AsyncClient) -> dict:
    """Propose a domain-verified application e-mail for a posting (deterministic).

    Returns {email, dedicated, generic, source_url}; email '' when the employer
    domain is unknown (board/ATS posting) or nothing verifiable was found."""
    host = _employer_host(job)
    if not host:
        return dict(_EMPTY)
    return await _lookup_on_host(client, host)


def _load_job(job_id: int):
    with db.db() as con:
        return db.get_job(con, job_id)


async def lookup_and_propose(job_id: int) -> dict:
    """Resolve one posting's application e-mail on demand. Proposes only — never
    writes contact_email; the human confirms before any send."""
    job = await asyncio.to_thread(_load_job, job_id)
    if job is None:
        return dict(_EMPTY)
    async with httpx.AsyncClient(
        headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT,
        max_redirects=_MAX_REDIRECTS,
    ) as client:
        return await lookup(job, client)
