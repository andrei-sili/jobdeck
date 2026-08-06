"""Data-layer behaviour of the job inbox page (no NiceGUI rendering)."""

import ast
import inspect
import pathlib
import time
from html.parser import HTMLParser

import pytest
from nicegui import ui
from nicegui.elements.markdown import prepare_content

from jobdeck.ui import helpers
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


def test_mappe_summary_names_the_size_it_started_from():
    """The attachment is the deliverability variable the user cannot see —
    a bare 'ready, 1.6 MB' hides that 3.7 MB went in. Shared by the queue and
    the job inbox, because the inbox used to report the bare size only."""
    shrunk = helpers.mappe_summary({
        "pages": 10, "size_bytes": 1_628_894, "size_before_bytes": 3_854_093,
        "compression": "3.68 MB → 1.55 MB (300 dpi, q85, 3 image(s))",
        "anlagen": ["01_zeugnis.pdf"],
    })
    assert shrunk == "Mappe ready: 10 pages, 1.6 MB (compressed from 3.7 MB) ✓"

    untouched = helpers.mappe_summary({
        "pages": 4, "size_bytes": 512_000, "size_before_bytes": 512_000,
        "compression": "", "anlagen": [],
    })
    assert untouched == "Mappe ready: 4 pages, 0.5 MB ✓"


def test_mappe_summary_lists_the_anlagen_for_the_job_inbox():
    with_anlagen = helpers.mappe_summary({
        "pages": 10, "size_bytes": 1_628_894, "size_before_bytes": 3_854_093,
        "compression": "3.68 MB → 1.55 MB (300 dpi, q85, 3 image(s))",
        "anlagen": ["01_zeugnis.pdf", "02_zertifikat.pdf"],
    }, with_anlagen=True)
    assert with_anlagen == (
        "Mappe ready: 10 pages, 1.6 MB (compressed from 3.7 MB) · "
        "Anlagen: 01_zeugnis.pdf, 02_zertifikat.pdf ✓"
    )

    none = helpers.mappe_summary({
        "pages": 1, "size_bytes": 100_000, "size_before_bytes": 100_000,
        "compression": "", "anlagen": [],
    }, with_anlagen=True)
    assert none == "Mappe ready: 1 pages, 0.1 MB · no Anlagen ✓"


def test_the_working_inbox_hides_both_piles_and_each_view_shows_one():
    # a mismatch violates a stated hard requirement, a dead posting's ad is
    # gone: both are facts about the posting, so both hide it — and opening one
    # pile is a separate VIEW, not a filter stacked on the other
    assert jobs._PILE_FILTERS[jobs.PILE_NONE] == {
        "mismatches": "exclude", "gone": "exclude"}
    assert jobs._PILE_FILTERS[jobs.PILE_MISMATCHES]["mismatches"] == "only"
    assert jobs._PILE_FILTERS[jobs.PILE_DEAD]["gone"] == "only"
    assert set(jobs._PILE_FILTERS) == set(jobs._EMPTY_VIEW)


@pytest.mark.parametrize("pile, mismatches, dead, expected", [
    (jobs.PILE_NONE, 129, 14, "129 mismatches hidden · 14 dead hidden"),
    (jobs.PILE_NONE, 0, 14, "14 dead hidden"),
    (jobs.PILE_NONE, 129, 0, "129 mismatches hidden"),
    (jobs.PILE_NONE, 0, 0, ""),
    # the pile on screen is not "hidden" any more; the other one still is
    (jobs.PILE_MISMATCHES, 129, 14, "14 dead hidden"),
    (jobs.PILE_DEAD, 129, 14, "129 mismatches hidden"),
])
def test_the_hidden_line_states_each_pile_separately(pile, mismatches, dead,
                                                     expected):
    # never a total: a posting can be both a mismatch and offline, so adding
    # the two would double-count it
    assert jobs._hidden_line(pile, mismatches, dead) == expected


@pytest.mark.parametrize("job, expected", [
    ({"match_score": 92, "effective_score": 72, "age_days": 61},
     " · match 92 → 72 · 61 Tage alt"),
    ({"match_score": 78, "effective_score": 78, "age_days": 1},
     " · match 78 · 1 Tage alt"),
    ({"match_score": 80, "effective_score": 80, "age_days": None},
     " · match 80 · Datum unbekannt"),
    ({"match_score": None, "effective_score": None, "age_days": 3}, ""),
])
def test_the_score_line_shows_what_age_cost(job, expected):
    # the arrow appears only when age actually took points off, and both numbers
    # come from the row the query returned — the one that decided the position
    assert jobs._score_line(job) == expected


def _seed_scored(con, count):
    from jobdeck import db
    for n in range(count):
        job_id = db.insert_job_if_new(con, {
            "source": "arbeitsagentur", "external_id": f"j{n}", "title": "Dev",
            "company": f"Firma {n}", "url": f"https://firma.de/{n}",
        })
        con.execute("UPDATE jobs SET match_score=? WHERE id=?", (n + 1, job_id))
    con.commit()


def test_every_posting_is_reachable_by_paging(con, data_dir):
    # the old hard limit of 100 left 187 of his 287 open postings unreachable
    _seed_scored(con, 120)
    seen, page = [], 0
    while True:
        view = jobs._load_jobs("new", jobs.PILE_NONE, page, collapse=False)
        assert view["total"] == 120
        seen += [r["id"] for r in view["rows"]]
        if page + 1 >= view["pages"]:
            break
        page += 1
    assert len(seen) == 120 and len(set(seen)) == 120
    assert page == 2 and view["pages"] == 3  # 50 + 50 + 20


def test_a_page_past_the_end_shows_the_last_one_instead_of_nothing(con, data_dir):
    # a filter change or a background poll can shrink the list under the user
    _seed_scored(con, 60)
    view = jobs._load_jobs("new", jobs.PILE_NONE, 99, collapse=False)
    assert view["page"] == 1 and len(view["rows"]) == 10
    empty = jobs._load_jobs("applied", jobs.PILE_NONE, 99, collapse=False)
    assert empty["page"] == 0 and empty["rows"] == [] and empty["total"] == 0


def test_paging_does_not_skip_or_repeat_a_row_at_the_boundary(con, data_dir):
    _seed_scored(con, 51)
    first = jobs._load_jobs("new", jobs.PILE_NONE, 0, collapse=False)
    second = jobs._load_jobs("new", jobs.PILE_NONE, 1, collapse=False)
    assert len(first["rows"]) == 50 and len(second["rows"]) == 1
    assert set(r["id"] for r in first["rows"]).isdisjoint(
        r["id"] for r in second["rows"])
    # best score first across the page break, not only inside a page
    assert first["rows"][0]["match_score"] == 51
    assert second["rows"][0]["match_score"] == 1


@pytest.mark.parametrize("page, total, shown, expected", [
    (0, 287, 50, "1–50 von 287"),
    (1, 287, 50, "51–100 von 287"),
    (5, 287, 37, "251–287 von 287"),
    (0, 3, 3, "1–3 von 3"),
    (0, 0, 0, ""),
])
def test_the_range_line_says_where_in_the_pipeline_this_page_sits(
    page, total, shown, expected
):
    assert jobs._range_line(page, total, shown) == expected


def _company_job(con, ext, company, score, published_on=""):
    from jobdeck import db
    job_id = db.insert_job_if_new(con, {
        "source": "arbeitsagentur", "external_id": ext, "title": f"Dev {ext}",
        "company": company, "url": f"https://firma.de/{ext}",
    })
    con.execute("UPDATE jobs SET match_score=?, published_on=? WHERE id=?",
                (score, published_on, job_id))
    return job_id


def test_a_company_takes_one_row_and_its_best_posting_represents_it(con, data_dir):
    # 36 companies held 83 of his 237 no-email postings, and only one
    # application per company is possible — 47 rows could never become one
    best = _company_job(con, "a1", "Sigtronic GmbH", 88)
    _company_job(con, "a2", "sigtronic gmbh ", 70)   # same company, spelled loosely
    _company_job(con, "a3", "SIGTRONIC GMBH", 60)
    other = _company_job(con, "b1", "Andere AG", 75)
    con.commit()

    view = jobs._load_jobs("new", jobs.PILE_NONE, 0)
    assert view["total"] == 2                     # companies, not postings
    assert [r["id"] for r in view["rows"]] == [best, other]
    head = view["rows"][0]
    assert head["company_count"] == 3
    siblings = view["siblings"][head["company_key"]]
    assert [r["match_score"] for r in siblings] == [70, 60]   # best-ranked first

    flat = jobs._load_jobs("new", jobs.PILE_NONE, 0, collapse=False)
    assert flat["total"] == 4 and flat["siblings"] == {}


def test_a_blank_company_never_groups_with_another(con, data_dir):
    # an empty employer field is missing data, not a company they share
    first = _company_job(con, "x1", "", 80)
    second = _company_job(con, "x2", "   ", 70)
    con.commit()
    view = jobs._load_jobs("new", jobs.PILE_NONE, 0)
    assert view["total"] == 2
    assert [r["company_count"] for r in view["rows"]] == [1, 1]
    assert [r["id"] for r in view["rows"]] == [first, second]


def test_the_group_that_represents_a_company_is_chosen_by_the_aged_score(
    con, data_dir
):
    import datetime
    today = datetime.date.today()
    stale_star = _company_job(con, "s1", "Firma", 92,
                              (today - datetime.timedelta(days=150)).isoformat())
    fresh_good = _company_job(con, "s2", "Firma", 78,
                              (today - datetime.timedelta(days=1)).isoformat())
    con.commit()
    view = jobs._load_jobs("new", jobs.PILE_NONE, 0)
    # 92 aged to 72 loses to a fresh 78: the row that represents the company is
    # the one the ordering actually prefers, not the one with the raw high score
    assert [r["id"] for r in view["rows"]] == [fresh_good]
    assert [r["id"] for r in view["siblings"][view["rows"][0]["company_key"]]] \
        == [stale_star]


def test_grouping_respects_the_hidden_piles(con, data_dir):
    from jobdeck import db
    keep = _company_job(con, "g1", "Firma", 80)
    mismatch = _company_job(con, "g2", "Firma", 0)
    dead = _company_job(con, "g3", "Firma", 90)
    con.execute("UPDATE jobs SET liveness='gone' WHERE id=?", (dead,))
    con.commit()

    view = jobs._load_jobs("new", jobs.PILE_NONE, 0)
    # the 90 is offline and the 0 violates a hard requirement: neither may
    # represent the company, and neither may be counted as one of its postings
    assert [r["id"] for r in view["rows"]] == [keep]
    assert view["rows"][0]["company_count"] == 1
    assert view["siblings"] == {}

    # each pile stays reachable as its own grouped view
    assert [r["id"] for r in jobs._load_jobs("new", jobs.PILE_DEAD, 0)["rows"]] \
        == [dead]
    assert [r["id"] for r in
            jobs._load_jobs("new", jobs.PILE_MISMATCHES, 0)["rows"]] == [mismatch]
    assert db.count_job_groups(con, "new") == 1
