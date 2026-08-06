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

import httpx

from jobdeck import apply_channel, db, netsafe

log = logging.getLogger(__name__)

_USER_AGENT = "JobDeck/0.1 (+https://github.com/andrei-sili/jobdeck)"
_TIMEOUT = 10.0
_MAX_REDIRECTS = 10
_MAX_BYTES = 400_000


def _is_robots_disallowed(url: str) -> bool:
    """True for a Jooble link this app must never request.

    de.jooble.org/robots.txt Disallows /away/ and /desc/ for `User-agent: *`
    (and /*?ckey=, which every stored /desc/ link carries). Both are exactly
    the URLs a feed result points at, so the polite thing is also the only
    thing: never fetch them, and hand the link to the HUMAN instead. Same
    call as Arbeitnow's disallowed apply route — see _resolve_arbeitnow.

    Following them used to look attractive because /away/ 3xx-redirects to
    the real posting; it is also Jooble's click-billing endpoint, so fetching
    it fires a paid click for a visit that never happens.
    """
    parts = netsafe.split_url(url if "://" in url else "https://" + url)
    if parts is None:
        return False
    host = (parts.hostname or "").lower()
    if not (host == "jooble.org" or host.endswith(".jooble.org")):
        return False
    path = parts.path.lower()
    return (path.startswith(("/away/", "/desc/", "/m/away/", "/m/desc/"))
            or "ckey=" in (parts.query or "").lower())


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


def _parse_arbeitnow_page(page_html: str) -> "_ArbeitnowPage | None":
    """Parse an untrusted job page off the event loop; None on any hiccup."""
    parser = _ArbeitnowPage()
    try:
        parser.feed(page_html)
        parser.close()
    except Exception:  # hostile/odd HTML — no signal, keep the fallback path
        return None
    return parser


def _arbeitnow_apply_href(hrefs: list[str], page_url: str) -> str:
    """THIS posting's own ``…/apply`` deep-link, '' when the layout changed.

    The href comes from an UNTRUSTED page, so it is accepted only when it is
    an https link to the SAME Arbeitnow site as the page it was read from,
    whose path is exactly this job's path + '/apply' — a planted anchor earlier
    in the document (employer-supplied description HTML, a 'related jobs'
    block) must not win, and a non-http scheme must never become a URL the app
    opens."""
    page_parts = netsafe.split_url(page_url)
    if page_parts is None:
        return ""
    site = apply_channel.arbeitnow_site(page_parts.hostname or "")
    if not site:
        return ""
    want = page_parts.path.rstrip("/") + "/apply"
    for href in hrefs:
        parts = netsafe.split_url(href)  # a poisoned href must not raise
        if parts is None:
            continue
        if (parts.scheme in ("http", "https")
                and apply_channel.arbeitnow_site(parts.hostname or "") == site
                and not parts.username and not parts.password
                and parts.path.rstrip("/") == want):
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
    parser = await asyncio.to_thread(_parse_arbeitnow_page, page)
    if parser is None:
        return None
    apply_href = _arbeitnow_apply_href(parser.hrefs, url)
    if "form_job_application" in parser.form_ids:
        return apply_href or url, apply_channel.ApplyChannel(
            apply_channel.CHANNEL_ATS, "JOIN")
    if apply_href:
        return apply_href, apply_channel.ApplyChannel(
            apply_channel.CHANNEL_BOARD, "Arbeitnow")
    return None


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
    # parsing untrusted HTML is CPU-bound; keep it off the NiceGUI event loop
    return await asyncio.to_thread(apply_channel.detect_ats_from_page, page)


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
    if url and apply_channel.is_arbeitnow_job(url):
        resolved = await _resolve_arbeitnow(client, url)
        if resolved is not None:
            return resolved
    final = url
    if url and _is_robots_disallowed(url):
        # Nothing is fetched: the board's own link is stored and the user
        # clicks it. classify() already reads it as board_apply/Jooble, which
        # is the honest answer anyway — the destination is another job board
        # about nine times in ten, not the employer.
        return final, apply_channel.classify(final, email)
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


BATCH_LIMIT = 60           # one pass; a second click continues the backlog
BATCH_PAUSE_S = 0.4        # between postings — this walks other people's sites


def _pending(limit: int):
    with db.db() as con:
        return [dict(r) for r in db.jobs_needing_apply_channel(con, limit)]


def _pending_count() -> int:
    with db.db() as con:
        return db.count_jobs_needing_apply_channel(con)


async def resolve_pending(limit: int = BATCH_LIMIT,
                          client: httpx.AsyncClient | None = None) -> dict:
    """Resolve the apply channel for a batch of postings, best-scored first.

    The per-job button is the right shape for one posting and the wrong shape
    for a backlog: of 287 scored postings only 8 had a channel, and the rest
    meant that many individual clicks. Most resolve from the URL alone with no
    request at all; the ones that do fetch are spaced out, run one at a time
    and go through the same netsafe guard as every other outbound call — this
    is a batch, so it must be gentler than a human clicking, not faster.

    Returns counters plus a channel breakdown. Never raises: one bad posting
    must not end the pass. `client` is injectable so a test can drive the whole
    pass through a MockTransport rather than the network.
    """
    jobs = await asyncio.to_thread(_pending, limit)
    counts: dict[str, int] = {}
    resolved = failed = 0
    if not jobs:
        return {"resolved": 0, "failed": 0, "remaining": 0, "channels": counts}
    owned = client is None
    if owned:
        client = httpx.AsyncClient(
            headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT,
            max_redirects=_MAX_REDIRECTS,
        )
    try:
        for index, job in enumerate(jobs):
            if index:
                await asyncio.sleep(BATCH_PAUSE_S)
            try:
                final, ch = await resolve(job, client)
                await asyncio.to_thread(_store, job["id"], ch, final)
            except Exception as exc:  # noqa: BLE001 - one posting, not the pass
                log.warning("apply-resolve: job %s failed: %s", job["id"], exc)
                failed += 1
                continue
            resolved += 1
            counts[ch.channel] = counts.get(ch.channel, 0) + 1
    finally:
        if owned:
            await client.aclose()
    remaining = await asyncio.to_thread(_pending_count)
    log.info("apply-resolve batch: %s resolved, %s failed, %s still pending",
             resolved, failed, remaining)
    return {"resolved": resolved, "failed": failed, "remaining": remaining,
            "channels": counts}


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
