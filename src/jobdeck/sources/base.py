"""Common contract for job board adapters."""

import re
from dataclasses import dataclass, field
from typing import Protocol

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Words that mark a posting as remote-friendly when found in title/description
REMOTE_MARKERS = ("remote", "home office", "homeoffice", "home-office", "100% remote")


class SourceUnavailable(Exception):
    """A source could not be queried (network, auth, or format change).

    Carries a short human-readable detail; the polling service stores it on
    the profile so the UI can show a degraded-source banner while the
    remaining sources keep flowing.
    """

    def __init__(self, source: str, detail: str):
        super().__init__(f"{source}: {detail}")
        self.source = source
        self.detail = detail


@dataclass
class SearchQuery:
    """Normalized search parameters, mapped by each adapter to its API."""

    keywords: str
    location: str = ""  # empty = nationwide
    radius_km: int = 0  # 0 = no radius restriction


@dataclass
class JobPosting:
    source: str
    external_id: str
    title: str = ""
    company: str = ""
    location: str = ""
    remote: bool = False
    url: str = ""
    description: str = ""
    contact_email: str = ""
    published_at: str = ""
    raw: dict = field(default_factory=dict)


class JobSource(Protocol):
    name: str

    async def search(self, query: SearchQuery) -> list[JobPosting]:
        """Return postings for the query. Raises SourceUnavailable on failure."""
        ...

    async def fetch_details(self, posting: JobPosting) -> JobPosting:
        """Enrich a posting with full description/contact. Best-effort:
        a failed detail fetch returns the posting unchanged."""
        ...


def extract_email(text: str) -> str:
    match = EMAIL_RE.search(text or "")
    # Postings often end sentences with the address ("... an hr@firma.de.")
    return match.group(0).rstrip(".") if match else ""


def looks_remote(*texts: str) -> bool:
    haystack = " ".join(t or "" for t in texts).lower()
    return any(marker in haystack for marker in REMOTE_MARKERS)


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").replace("&nbsp;", " ").strip()
