"""Application-channel classification for a posting's apply URL.

Slice #1 of contact-resolution: a deterministic, no-I/O, no-LLM classifier over
the ``url`` we already store. It answers "WHERE does one apply?" — a direct
company e-mail, a known ATS / e-recruiting portal (Personio, softgarden, JOIN,
rexx, Workday…), the job board itself, or the employer's own site — so the UI
can label the channel and deep-link the apply page. The German market is
form/ATS-first (research 2026-07-18): most no-email postings land on an ATS or a
board, never an auto-sendable inbox.

Portals/ATS are NEVER auto-submitted (platform AGB bot-bans, Art. 22 DSGVO);
only a DIRECT_EMAIL is eligible for the Gmail auto-send path. This module ONLY
classifies — no network, no side effects, no actions. Following aggregator
redirects (jooble/arbeitnow) and web e-mail lookup are later slices.
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from jobdeck import netsafe

# Channel vocabulary — a subset of the full cascade enum; the rest (RECRUITER,
# IMPRESSUM_ONLY, PHONE_POSTAL) needs the web-lookup slice.
CHANNEL_DIRECT_EMAIL = "direct_email"    # a company e-mail we hold -> auto-send eligible
CHANNEL_ATS = "ats_form"                 # a known ATS/e-recruiting portal -> open + apply
CHANNEL_BOARD = "board_apply"            # apply through the job board itself
CHANNEL_COMPANY_SITE = "company_site"    # employer's own page (likely a form) -> open + apply
CHANNEL_UNKNOWN = "unknown"              # no usable URL


@dataclass(frozen=True)
class ApplyChannel:
    """The resolved channel plus a human label for the ATS/board when known."""

    channel: str
    vendor: str = ""


# ATS / e-recruiting vendors matched by host suffix (+ optional path). German-
# native cluster first (covers most of the ~79% no-email postings). DATA, not
# code — extend as new vendors surface in the corpus. CAVEAT: a CNAME custom
# career domain (jobs.company.de) hides the vendor from the LANDING host and
# falls through to COMPANY_SITE; `detect_ats_from_page` then inspects the
# fetched page's form/script/iframe hosts for the same vendors.
_ATS = (
    ("Personio", r"(?:^|\.)jobs\.personio\.(?:de|com)$", None),
    ("softgarden", r"(?:^|\.)(?:career\.softgarden\.de|softgarden\.io)$", None),
    ("concludis", r"(?:^|\.)concludis\.de$", None),
    ("rexx systems", r"(?:^|\.)rexx-systems\.com$", None),
    ("d.vinci", r"(?:^|\.)dvinci-(?:hr|easy)\.com$", None),
    ("onlyfy", r"(?:^|\.)(?:onlyfy\.jobs|jobbase\.io|prescreen\.io)$", None),
    ("HR4YOU", r"(?:^|\.)hr4you\.org$", None),
    ("BeeSite", r"(?:^|\.)beesite\.de$", None),
    ("Kenjo", r"(?:^|\.)kenjo\.io$", None),
    ("JOIN", r"(?:^|\.)join\.com$", r"^/(?:companies|jobs)/"),
    ("BITE", r"(?:^|\.)bewerbermanagement\.net$", None),
    ("EmmySoft", r"(?:^|\.)emmysoft\.com$", None),
    ("Workday", r"(?:^|\.)myworkdayjobs\.com$", None),
    ("SuccessFactors", r"(?:^|\.)successfactors\.(?:eu|com)$", None),
    ("Greenhouse", r"(?:^|\.)greenhouse\.io$", None),
    ("Lever", r"(?:^|\.)lever\.co$", None),
    ("SmartRecruiters", r"(?:^|\.)smartrecruiters\.com$", None),
    ("Ashby", r"(?:^|\.)ashbyhq\.com$", None),
    ("Recruitee", r"(?:^|\.)recruitee\.com$", None),
    ("Teamtailor", r"(?:^|\.)teamtailor\.com$", None),
    ("iCIMS", r"(?:^|\.)icims\.com$", None),
    ("Taleo", r"(?:^|\.)taleo\.net$", None),
    ("Workable", r"(?:^|\.)workable\.com$", None),
)
_ATS_RULES = tuple(
    (v, re.compile(h, re.I), re.compile(p, re.I) if p else None) for v, h, p in _ATS
)

# Job boards / aggregators we ingest from or that host the apply themselves.
_BOARDS = (
    ("Arbeitsagentur", r"(?:^|\.)arbeitsagentur\.de$"),
    ("Jooble", r"(?:^|\.)jooble\.org$"),
    # Arbeitnow serves its UK listings from a second TLD whose pages, markup and
    # robots.txt are identical to the .com one (byte-identical robots, verified
    # 2026-08-06). Without it 13 of his postings read as the employer's own site
    # and earn a page inspection the board never needed.
    ("Arbeitnow", r"(?:^|\.)arbeitnow\.(?:com|co\.uk)$"),
    # aggregators the Arbeitsagentur feed points at via externeURL — without an
    # entry they classify as company_site and earn a pointless page inspection
    ("get in IT", r"(?:^|\.)get-in-it\.de$"),
    ("GermanTechJobs", r"(?:^|\.)germantechjobs\.de$"),
    ("Persy", r"(?:^|\.)persy\.jobs$"),
    ("Deutschland-Stellenmarkt", r"(?:^|\.)deutschland-stellenmarkt\.de$"),
    ("Studyflix", r"(?:^|\.)studyflix\.de$"),
    ("StepStone", r"(?:^|\.)stepstone\.de$"),
    ("Indeed", r"(?:^|\.)indeed\.(?:com|de)$"),
    ("XING", r"(?:^|\.)xing\.com$"),
    ("LinkedIn", r"(?:^|\.)linkedin\.com$"),
    ("AMS", r"(?:^|\.)jobs\.ams\.at$"),
)
_BOARD_RULES = tuple((label, re.compile(h, re.I)) for label, h in _BOARDS)


# The board runs one site per market — its UK listings live on .co.uk, with the
# same markup, the same `…/apply` route and a byte-identical robots.txt.
_ARBEITNOW_SITES = ("arbeitnow.com", "arbeitnow.co.uk")


def arbeitnow_site(host: str) -> str:
    """Which Arbeitnow site a host belongs to, '' when it is not one.

    Collapses the `www.` variant so a page and its own apply link compare equal
    however the feed spelled them, while keeping the two markets distinct: a
    .co.uk page must not adopt an apply link pointing at .com."""
    host = (host or "").lower().removeprefix("www.")
    return host if host in _ARBEITNOW_SITES else ""


def is_arbeitnow_job(url: str) -> bool:
    """True for an Arbeitnow JOB page — the one route of theirs robots.txt
    allows a bot to request (the `…/apply` deep-link is disallowed)."""
    parts = netsafe.split_url(url if "://" in url else "https://" + url)
    if parts is None:
        return False
    return bool(arbeitnow_site(parts.hostname or "")) \
        and parts.path.startswith("/jobs/")


def _hostname(url: str) -> tuple[str, str]:
    """(lowercased host, path) for a URL, tolerating a missing scheme. A
    malformed URL yields no host, so it classifies as UNKNOWN rather than
    raising into the caller."""
    raw = (url or "").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    parts = netsafe.split_url(raw)
    if parts is None:
        return "", ""
    return (parts.hostname or "").lower(), parts.path or ""


def classify(url: str, contact_email: str = "") -> ApplyChannel:
    """Classify the apply channel from the stored URL (+ any known e-mail).

    A direct company e-mail wins — it is the only auto-sendable channel; else the
    host is matched against the ATS registry, then the board list; anything else
    is treated as the employer's own site (most likely a form)."""
    if (contact_email or "").strip():
        return ApplyChannel(CHANNEL_DIRECT_EMAIL)
    host, path = _hostname(url)
    if not host:
        return ApplyChannel(CHANNEL_UNKNOWN)
    for vendor, host_re, path_re in _ATS_RULES:
        if host_re.search(host) and (path_re is None or path_re.search(path)):
            return ApplyChannel(CHANNEL_ATS, vendor)
    for label, host_re in _BOARD_RULES:
        if host_re.search(host):
            return ApplyChannel(CHANNEL_BOARD, label)
    return ApplyChannel(CHANNEL_COMPANY_SITE)


# Vendor fingerprints for PAGE CONTENT (form-action / script-src / iframe-src
# hosts). Coarser than the landing-host registry ON PURPOSE: an embedded
# resource from ANY vendor subdomain (cdn., api., widget…) betrays the
# platform, while the landing registry must stay precise to label the page a
# user is standing on. Anchors are deliberately NOT inspected — a footer link
# to a vendor is marketing, not an application form.
_ATS_CONTENT = (
    ("Personio", ("personio.de", "personio.com")),
    ("softgarden", ("softgarden.de", "softgarden.io")),
    ("concludis", ("concludis.de",)),
    ("rexx systems", ("rexx-systems.com",)),
    ("d.vinci", ("dvinci-hr.com", "dvinci-easy.com")),
    ("onlyfy", ("onlyfy.jobs", "jobbase.io", "prescreen.io")),
    ("HR4YOU", ("hr4you.org",)),
    ("BeeSite", ("beesite.de",)),
    ("Kenjo", ("kenjo.io",)),
    ("JOIN", ("join.com",)),
    ("BITE", ("bewerbermanagement.net", "b-ite.de")),
    ("EmmySoft", ("emmysoft.com",)),
    ("Workday", ("myworkdayjobs.com", "workday.com")),
    ("SuccessFactors", ("successfactors.eu", "successfactors.com")),
    ("Greenhouse", ("greenhouse.io",)),
    ("Lever", ("lever.co",)),
    ("SmartRecruiters", ("smartrecruiters.com",)),
    ("Ashby", ("ashbyhq.com",)),
    ("Recruitee", ("recruitee.com",)),
    ("Teamtailor", ("teamtailor.com",)),
    ("iCIMS", ("icims.com",)),
    ("Taleo", ("taleo.net",)),
    ("Workable", ("workable.com",)),
)

# Attribute that carries the fingerprint URL, per inspected tag.
_MARKER_ATTRS = {"form": "action", "script": "src", "iframe": "src"}


class _MarkerParser(HTMLParser):
    """Collects form-action / script-src / iframe-src URLs in document order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag, attrs):
        wanted = _MARKER_ATTRS.get(tag)
        if wanted:
            for name, value in attrs:
                if name == wanted and value:
                    self.urls.append(value)


def _content_vendor(host: str) -> str:
    for vendor, domains in _ATS_CONTENT:
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return vendor
    return ""


def detect_ats_from_page(page_html: str) -> ApplyChannel | None:
    """Detect an ATS behind a custom career domain from the page's own markup.

    A CNAME'd career site (jobs.firma.de) looks like COMPANY_SITE by hostname,
    but its apply form posts to — or loads its widget from — the vendor's
    domain. Deterministic and offline: no network, the caller fetches. Returns
    the ATS channel on the first fingerprint match, else None (the page is
    UNTRUSTED — a non-match must never downgrade an existing classification)."""
    parser = _MarkerParser()
    try:
        parser.feed(page_html or "")
        parser.close()
    except Exception:  # tolerate any parser hiccup on hostile HTML — no match
        return None
    for url in parser.urls:
        host = netsafe.url_hostname(url)  # never raises on a poisoned URL
        if not host:
            continue  # relative or malformed — no vendor signal
        vendor = _content_vendor(host)
        if vendor:
            return ApplyChannel(CHANNEL_ATS, vendor)
    return None
