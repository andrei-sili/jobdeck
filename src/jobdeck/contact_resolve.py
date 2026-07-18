"""Deterministic e-mail resolution from a fetched company page (Impressum /
Karriere).

For a posting with no direct e-mail, pick a domain-verified DEDICATED
application address (bewerbung@ / karriere@ / jobs@ …) so it can — with human
confirmation — feed the auto-send path. FAIL-CLOSED: the address is derived from
an UNTRUSTED page, so it is accepted only when its registrable domain matches the
company's own domain (ASCII-only, so a non-ASCII / punycode homograph is
rejected). A generic info@ / kontakt@ is DEMOTED — never promoted to a send
target (an Impressum mailbox is not an application address; UWG/DSGVO). The
proposed address is never auto-used; a human confirms it in the review queue.
"""

import re
from urllib.parse import urlsplit

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Local-parts that signal a dedicated application inbox (best first).
_DEDICATED = ("bewerbung", "karriere", "jobs", "job", "recruiting", "career",
              "careers", "personal", "hr")
# Generic mailboxes that are NOT an application address — demoted, never auto-send.
_GENERIC = ("info", "kontakt", "contact", "office", "mail", "hallo", "service",
            "sekretariat", "empfang", "zentrale", "noreply", "no-reply")


def registrable_domain(host_or_url: str) -> str:
    """Naive eTLD+1 (last two labels), lowercased. Correct for the .de / .com
    domains German employers use; a public-suffix list (.co.uk etc.) is a later
    hardening. Empty for a non-ASCII host — a homograph/punycode guard, since a
    verified match must be exact ASCII."""
    raw = (host_or_url or "").strip()
    if "://" not in raw and not raw.startswith("//"):
        raw = "//" + raw  # give urlsplit a netloc to parse
    host = urlsplit(raw).hostname or ""
    host = host.lower().rstrip(".")
    if not host or not host.isascii() or host.startswith("xn--") or ".xn--" in host:
        return ""
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else ""


def _local_part(email: str) -> str:
    return email.split("@", 1)[0].lower()


def _is_dedicated(email: str) -> bool:
    return any(_local_part(email).startswith(p) for p in _DEDICATED)


def _is_generic(email: str) -> bool:
    return any(_local_part(email).startswith(p) for p in _GENERIC)


def _rank(email: str) -> int:
    """Lower is better: a dedicated application inbox beats a plain address, and a
    generic mailbox is worst (returned only as a demoted fallback)."""
    if _is_dedicated(email):
        return 0
    if _is_generic(email):
        return 2
    return 1


def resolve_email(page_text: str, company: str) -> dict:
    """Pick the best domain-verified e-mail from a fetched company page.

    `company` is the employer's host or URL — its registrable domain is the trust
    anchor. Returns {email, dedicated, generic}: `email` is '' when nothing on the
    company's own registrable domain is found; `dedicated` marks a real
    application inbox (bewerbung@ …) eligible — after human confirm — for the
    auto-send path; `generic` flags an info@-style fallback the caller must NOT
    auto-send to."""
    empty = {"email": "", "dedicated": False, "generic": False}
    target = registrable_domain(company)
    if not target:
        return empty
    seen, matched = set(), []
    for m in _EMAIL_RE.finditer(page_text or ""):
        email = m.group(0).rstrip(".").lower()
        if email in seen:
            continue
        seen.add(email)
        if registrable_domain(email.rsplit("@", 1)[-1]) == target:
            matched.append(email)
    if not matched:
        return empty
    best = min(matched, key=_rank)
    return {"email": best, "dedicated": _is_dedicated(best),
            "generic": _is_generic(best)}
