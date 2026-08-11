"""Small UI utilities shared by pages."""

import subprocess
import sys

from jobdeck import netsafe


def openable_url(url: str) -> str:
    """A stored URL the browser may be handed, '' when there is none safe.

    The single gate in front of every `ui.navigate.to`. Stored URLs come from
    board feeds, employer-supplied fields and resolved apply links — untrusted
    in every case — and NiceGUI turns navigate.to into window.open in the app's
    own origin, so a `javascript:` URL would execute there. Every page must go
    through here rather than reimplementing the check per button."""
    return url if netsafe.is_openable(url or "") else ""


def applied_line(already: dict) -> str:
    """'⚠ Bei X hast du dich bereits beworben (am 12.06. · Absage)'.

    Only ONE application per company is possible — the send path refuses a
    second one inside its own claim (services/send.py) — so a posting at such a
    company can never become an application. It says WHICH application and how
    it ended, because that is the difference between a company still deciding
    and one that already said no.

    Shared by the job inbox and the review queue: two wordings of one gate is
    how a screen ends up telling him something the send path will not do.
    """
    parts = []
    when = str(already.get("gesendet_am") or "")[:10]
    if when:
        parts.append(f"am {when}")
    status = str(already.get("status") or "").strip()
    if status:
        parts.append(status)
    detail = f" ({' · '.join(parts)})" if parts else ""
    return (f"⚠ Bei {already.get('firma') or 'dieser Firma'} hast du dich "
            f"bereits beworben{detail} — eine Bewerbung pro Firma.")


def mappe_summary(result: dict, *, with_anlagen: bool = False) -> str:
    """Confirmation line for a finished Mappe, for every page that builds one.

    It names the size the Mappe started from whenever it was shrunk: the
    attachment is the one deliverability variable the user cannot otherwise
    see, and it is the first suspect whenever an application lands in spam.
    Shared rather than written per page — the job inbox used to report a bare
    size and quietly hid that the document had been recompressed at all.
    """
    size_mb = result["size_bytes"] / 1024 / 1024
    shrunk = ""
    if result["compression"]:
        before_mb = result["size_before_bytes"] / 1024 / 1024
        shrunk = f" (compressed from {before_mb:.1f} MB)"
    anlagen = ""
    if with_anlagen:
        anlagen = (" · Anlagen: " + ", ".join(result["anlagen"])
                   if result["anlagen"] else " · no Anlagen")
    return (f"Mappe ready: {result['pages']} pages, "
            f"{size_mb:.1f} MB{shrunk}{anlagen} ✓")


def posting_markdown(text: str) -> str:
    """Posting text made safe to render as Markdown.

    A posting is scraped from a job board, so its text is untrusted, and
    Markdown carries raw HTML through by design. NiceGUI sanitizes the result
    client-side (it overrides `setHTML` with DOMPurify), which does stop script
    execution — verified: `<script>`, every `on*` handler and every
    `javascript:`/`data:` href are removed. But DOMPurify's default allowlist
    KEEPS `<style>`, and a style element is not scoped to where it sits: a
    posting carrying `body{display:none}` blanked the entire application
    (observed in the running app), and rules that merely REPOSITION elements
    are the more interesting version — this UI has a Send button.

    Escaping '<' means no tag can form in the first place, so the tag surface
    stops depending on someone else's allowlist. '&' is deliberately left
    alone: postings really do carry entities like `&amp;` (43 of his stored
    ones) and an entity cannot open a tag. Markdown links, emphasis and
    paragraphs are untouched, which is what postings actually use.

    Two layers, and it is worth being exact about which owns what. TAGS are
    ours, here. LINK SCHEMES are the framework's: `[x](javascript:…)` is
    Markdown, not a tag, so it still becomes an anchor and DOMPurify is what
    drops the href (verified in the running app). That split is deliberate —
    hand-rolling a Markdown link parser inside a security boundary would add
    more bypasses than it closes — so the framework's sanitizer must stay ON,
    which a test pins. A dangerous link also needs a human click, while a
    `<style>` rule fires on render.

    Escaping tags is not enough on its own, because Markdown GENERATES markup
    from syntax that contains no '<' at all. Two constructs have to go:

    * `![alt](url)` becomes a real `<img>` that the browser fetches on render —
      a read receipt (IP, time, which posting) for whoever wrote the posting,
      and Quasar renders every expansion's content eagerly, so 100 postings
      would fire 100 requests on page load without a single click. The '!' is
      escaped away, leaving an ordinary link the user may follow deliberately.
    * a run of '>' is nested blockquotes, and Markdown recurses once per level:
      195 of them cost 1.7 s of blocked event loop and ~199 raise
      RecursionError, which surfaces as HTTP 500 for the WHOLE job inbox. With
      '>' escaped the same input renders in 4 ms. Exactly one of his 332
      postings uses a blockquote, so the formatting loss is negligible.

    Known cosmetic limit: inside a fenced or indented CODE BLOCK the escapes are
    shown literally (`&lt;` instead of `<`), because Markdown escapes code
    content again. Measured on his 332 stored postings: none contains '<' at
    all and two contain '>', so nothing real is affected today.
    """
    escaped = (text or "").replace("<", "&lt;").replace(">", "&gt;")
    # inserting the backslash cannot re-form the construct the way deleting the
    # '!' would ("!![x]" -> "![x]" is an image again)
    return escaped.replace("![", "!\\[")


def open_in_system(path: str) -> None:
    """Open a file or folder with the system's default application.

    The UI runs on the same machine as the browser (local-first app), so
    spawning the desktop opener is the natural way to show documents.
    """
    try:
        if sys.platform.startswith("win"):
            import os

            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass
