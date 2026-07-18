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
import re
from urllib.parse import urlsplit

import httpx

from jobdeck import apply_channel, contact_resolve, db
from jobdeck.ai import llm

log = logging.getLogger(__name__)

_DOMAIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9-]+)+", re.I)

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


def _extract_domain(text: str) -> str:
    """First plausible registrable domain in the model's short answer, '' if the
    model replied 'none' or gave nothing usable."""
    for m in _DOMAIN_RE.finditer(text or ""):
        cand = contact_resolve.registrable_domain(m.group(0))
        if cand:
            return cand
    return ""


def discover_domain(company: str, location: str) -> tuple[str, "llm.LLMResult | None"]:
    """Use one web-search call to find the employer's official domain. Returns
    (domain, usage); usage is metered by the caller. AI spend — gated by the
    web_contact_search toggle upstream."""
    if not (company or "").strip():
        return "", None
    where = f" in {location}" if (location or "").strip() else ""
    prompt = (
        f'Find the OFFICIAL company website of the German employer "{company}"'
        f'{where}. Use web search. Reply with ONLY the website\'s registrable '
        f"domain (e.g. firma.de) and nothing else. If you cannot determine it "
        f"with confidence, reply exactly: none"
    )
    try:
        res = llm.web_search(prompt, max_tokens=1024)  # room for search + answer
    except llm.LLMError as exc:
        log.info("contact-lookup: domain discovery failed: %s", exc)
        return "", None
    return _extract_domain(res.text), res


def _record_usage(usage: "llm.LLMResult") -> None:
    with db.db() as con:
        db.record_llm_usage(con, usage.input_tokens, usage.output_tokens,
                            usage.cost_usd)


async def lookup(job, client: httpx.AsyncClient, *, ai_search: bool = False) -> dict:
    """Propose a domain-verified application e-mail for a posting.

    Deterministic first (the employer's own site). When that finds nothing and
    ai_search is on, one web-search call discovers the employer domain, then the
    same deterministic Impressum lookup runs on it. Returns {email, dedicated,
    generic, source_url}; email '' when nothing verifiable was found."""
    host = _employer_host(job)
    if host:
        r = await _lookup_on_host(client, host)
        if r["email"]:
            return r
    if ai_search:
        domain, usage = await asyncio.to_thread(
            discover_domain, job["company"], job["location"])
        if usage is not None:
            await asyncio.to_thread(_record_usage, usage)
        if domain:
            r = await _lookup_on_host(client, domain)
            if r["email"]:
                return r
    return dict(_EMPTY)


def _load_job(job_id: int):
    with db.db() as con:
        return db.get_job(con, job_id)


def _ai_search_enabled() -> bool:
    """The per-install web-contact-search toggle (default off) — the on/off
    button that authorises the AI web-search fallback and its spend."""
    with db.db() as con:
        return db.get_setting(con, "web_contact_search", "0") == "1"


async def lookup_and_propose(job_id: int) -> dict:
    """Resolve one posting's application e-mail on demand. Proposes only — never
    writes contact_email; the human confirms before any send. The AI web-search
    fallback runs only when the web_contact_search toggle is on."""
    job = await asyncio.to_thread(_load_job, job_id)
    if job is None:
        return dict(_EMPTY)
    ai = await asyncio.to_thread(_ai_search_enabled)
    async with httpx.AsyncClient(
        headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT,
        max_redirects=_MAX_REDIRECTS,
    ) as client:
        return await lookup(job, client, ai_search=ai)
