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
from urllib.parse import urlsplit

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
# career domain (jobs.company.de) hides the vendor and falls through to
# COMPANY_SITE; catching that needs the form-action/script-src inspection of a
# later slice.
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
    ("Arbeitnow", r"(?:^|\.)arbeitnow\.com$"),
    ("StepStone", r"(?:^|\.)stepstone\.de$"),
    ("Indeed", r"(?:^|\.)indeed\.(?:com|de)$"),
    ("XING", r"(?:^|\.)xing\.com$"),
    ("LinkedIn", r"(?:^|\.)linkedin\.com$"),
    ("AMS", r"(?:^|\.)jobs\.ams\.at$"),
)
_BOARD_RULES = tuple((label, re.compile(h, re.I)) for label, h in _BOARDS)


def _hostname(url: str) -> tuple[str, str]:
    """(lowercased host, path) for a URL, tolerating a missing scheme."""
    raw = (url or "").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
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
