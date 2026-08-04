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
    """
    return (text or "").replace("<", "&lt;")


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
