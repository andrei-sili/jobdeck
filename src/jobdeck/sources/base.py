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
    # Structured facts a source states about the posting, in the jobs table's
    # own vocabulary (work address, pay range, Arbeitnehmerüberlassung). A bag
    # rather than ten fields: every source states a different subset, and the
    # writer that stores them ignores what nobody stated.
    facts: dict = field(default_factory=dict)
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


def _is_word(ch: str) -> bool:
    """`\\w` for str patterns: alphanumeric per str.isalnum(), plus underscore."""
    return ch.isalnum() or ch == "_"


def extract_email(text: str) -> str:
    """First address matching `[\\w.+-]+@[\\w-]+\\.[\\w.-]+`, '' when there is none.

    Scans outward from each '@' rather than letting a regex retry every start
    position: `[\\w.+-]+` backtracks quadratically across a long run of word
    characters that never reaches an '@' (measured 13.6 s at 60 KB), and the
    BA-hosted description this reads has no length cap. Equivalence with the
    regex is pinned by a fuzz test — treat the two as one contract.
    """
    text = text or ""
    at = text.find("@")
    while at >= 0:
        start = at
        while start > 0 and (_is_word(text[start - 1]) or text[start - 1] in ".+-"):
            start -= 1
        end = at + 1
        while end < len(text) and (_is_word(text[end]) or text[end] == "-"):
            end += 1
        # local part and domain label are both `+` (at least one character),
        # and the label must be followed by a literal dot then a `+` tail
        if start < at and end > at + 1 and end < len(text) and text[end] == ".":
            tail = end + 1
            while tail < len(text) and (_is_word(text[tail]) or text[tail] in ".-"):
                tail += 1
            if tail > end + 1:
                # postings often end the sentence on the address ("an hr@firma.de.")
                return text[start:tail].rstrip(".")
        at = text.find("@", at + 1)
    return ""


def looks_remote(*texts: str) -> bool:
    haystack = " ".join(t or "" for t in texts).lower()
    return any(marker in haystack for marker in REMOTE_MARKERS)


def strip_html(text: str) -> str:
    """Replace every `<[^>]+>` tag with a space, in one left-to-right pass.

    The regex form backtracks quadratically on a run of '<' that never reaches
    a '>': at the 400 KB cap the guarded page fetch allows, a body of
    "<" * 400_000 — 424 bytes on the wire once gzipped — burned ~52 s of CPU.
    The pass exploits what the engine cannot: once no '>' follows a '<', no tag
    can close from any later position either, so scanning stops instead of
    retrying. Output is identical to the regex, fuzz-pinned by a test.
    """
    text = text or ""
    out: list[str] = []
    i = 0
    while True:
        lt = text.find("<", i)
        if lt < 0:
            out.append(text[i:])
            break
        gt = text.find(">", lt + 1)
        if gt < 0:  # nothing closes from here on — the rest is text
            out.append(text[i:])
            break
        if gt == lt + 1:  # "<>" is no tag: `[^>]+` demands a character
            out.append(text[i:gt + 1])
            i = gt + 1
            continue
        out.append(text[i:lt])
        out.append(" ")
        i = gt + 1
    return "".join(out).replace("&nbsp;", " ").strip()
