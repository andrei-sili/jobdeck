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


def drafting_module():
    from jobdeck.services import drafting
    return drafting


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


def test_the_working_inbox_hides_every_pile_and_each_view_shows_one():
    # a mismatch violates a stated hard requirement, a dead posting's ad is
    # gone, and a posting at a company already applied to can never become an
    # application: all three are FACTS about the posting, so all three hide it
    # — and opening one pile is a separate VIEW, not a filter stacked on another
    assert jobs._PILE_FILTERS[jobs.PILE_NONE] == {
        "mismatches": "exclude", "gone": "exclude", "applied": "exclude"}
    assert jobs._PILE_FILTERS[jobs.PILE_MISMATCHES]["mismatches"] == "only"
    assert jobs._PILE_FILTERS[jobs.PILE_DEAD]["gone"] == "only"
    assert jobs._PILE_FILTERS[jobs.PILE_APPLIED]["applied"] == "only"
    for pile, filters in jobs._PILE_FILTERS.items():
        only = [k for k, v in filters.items() if v == "only"]
        assert len(only) == (0 if pile == jobs.PILE_NONE else 1), pile
    assert set(jobs._PILE_FILTERS) == set(jobs._EMPTY_VIEW) == set(jobs.PILE_LABELS)


@pytest.mark.parametrize("pile, status, mismatches, dead, applied, expected", [
    (jobs.PILE_NONE, "new", 129, 58, 30,
     "129 mismatches hidden · 58 dead hidden · 30 bei schon beworbenen Firmen hidden"),
    (jobs.PILE_NONE, "new", 0, 58, 0, "58 dead hidden"),
    (jobs.PILE_NONE, "new", 129, 0, 0, "129 mismatches hidden"),
    (jobs.PILE_NONE, "new", 0, 0, 0, ""),
    (jobs.PILE_APPLIED, "new", 129, 58, 30,
     "30 Stellen bei Firmen, bei denen du dich schon beworben hast"),
    # a pile view INCLUDES the other pile, so it must not claim to hide it — the
    # label is derived from the filters the query really used
    (jobs.PILE_MISMATCHES, "new", 129, 58, 30,
     "129 mismatches — hard requirement violated"),
    (jobs.PILE_DEAD, "new", 129, 58, 30, "58 postings whose ad is gone"),
    # a view of postings he has already acted on hides nothing at all
    (jobs.PILE_NONE, "portal", 129, 58, 30, ""),
    (jobs.PILE_NONE, "applied", 129, 58, 30, ""),
])
def test_the_hidden_line_can_never_contradict_the_list(pile, status, mismatches,
                                                       dead, applied, expected):
    # never a total either: a posting can be both a mismatch and offline, so
    # adding the two would double-count it
    filters = jobs._view_filters(pile, status)
    assert jobs._hidden_line(filters, mismatches, dead, applied) == expected


def test_a_posting_he_has_acted_on_is_never_hidden_from_its_own_view(con, data_dir):
    """The row of a posting in `portal` carries the button that finishes the job
    ("I applied — record it"). Hiding it for being offline would hide that."""
    from jobdeck import db
    job_id = _company_job(con, "p1", "Firma", 90)
    con.execute("UPDATE jobs SET status='portal', liveness='gone' WHERE id=?",
                (job_id,))
    con.commit()
    portal = jobs._load_jobs("portal", jobs.PILE_NONE, 0)
    assert [r["id"] for r in portal["rows"]] == [job_id]
    assert portal["filters"] == {"mismatches": "include", "gone": "include",
                                 "applied": "include"}
    # while the working inbox still hides it
    assert db.count_jobs(con, "new", gone="exclude") == 0


@pytest.mark.parametrize("job, expected", [
    ({"match_score": 92, "effective_score": 72, "age_days": 61},
     " · match 92 → 72 · 61 Tage alt"),
    ({"match_score": 78, "effective_score": 78, "age_days": 1},
     " · match 78 · 1 Tag alt"),
    ({"match_score": 70, "effective_score": 70, "age_days": 0},
     " · match 70 · heute"),
    # the boards state no timezone, so a posting can legitimately read as -1
    ({"match_score": 70, "effective_score": 70, "age_days": -1},
     " · match 70 · heute"),
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


@pytest.mark.parametrize("page, total, shown, collapse, expected", [
    (0, 266, 50, True, "1–50 von 266 Firmen"),
    (1, 266, 50, True, "51–100 von 266 Firmen"),
    (0, 287, 50, False, "1–50 von 287 Stellen"),
    (5, 287, 37, False, "251–287 von 287 Stellen"),
    (0, 3, 3, True, "1–3 von 3 Firmen"),
    (0, 0, 0, True, ""),
])
def test_the_range_line_names_the_unit_it_counts(page, total, shown, collapse,
                                                 expected):
    # the unit changes with the grouping toggle while the pile counts beside it
    # stay postings; an unlabelled pair of numbers invites comparing them
    assert jobs._range_line(page, total, shown, collapse) == expected


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


def test_one_employer_cannot_decide_how_much_a_page_renders(con, data_dir):
    from jobdeck import db
    for n in range(30):
        _company_job(con, f"m{n}", "Massenposter GmbH", 90 - n)
    con.commit()
    view = jobs._load_jobs("new", jobs.PILE_NONE, 0)
    assert view["total"] == 1
    head = view["rows"][0]
    assert head["company_count"] == 30            # the truth is still reported
    siblings = view["siblings"][head["company_key"]]
    assert len(siblings) == db.SIBLINGS_PER_COMPANY   # but the render is bounded
    assert [r["match_score"] for r in siblings] == list(range(89, 79, -1))


def test_the_grouping_toggle_never_reorders_the_all_statuses_view(con, data_dir):
    # 'all' mixes statuses and is ordered newest-first; flipping the toggle must
    # not silently switch the page to a score ordering. The scores DISAGREE with
    # the ids on purpose — with them aligned the test could not fail.
    best = _company_job(con, "z1", "Alpha", 95)     # oldest row, highest score
    middle = _company_job(con, "z2", "Beta", 10)
    newest = _company_job(con, "z3", "Gamma", 50)   # newest row, middling score
    con.commit()
    by_id = [newest, middle, best]                  # id DESC
    flat = jobs._load_jobs("all", jobs.PILE_NONE, 0, collapse=False)
    grouped = jobs._load_jobs("all", jobs.PILE_NONE, 0)
    assert [r["id"] for r in flat["rows"]] == by_id
    assert [r["id"] for r in grouped["rows"]] == by_id
    # and the 'new' view DOES order on the aged score, so the two are not the
    # same query with a different name
    assert [r["id"] for r in jobs._load_jobs("new", jobs.PILE_NONE, 0)["rows"]] \
        == [best, newest, middle]


def test_companies_group_the_way_the_duplicate_gate_compares_them(con, data_dir):
    """dedupe.py exists because SQLite's lower() folds ASCII only. The grouped
    view claims "one application per company", so it must group by the very
    function that enforces that — otherwise it shows two rows for one company
    and the second application is refused after he has written it."""
    from jobdeck.dedupe import find_duplicate_bewerbung
    best = _company_job(con, "u1", "MÜLLER Software GmbH", 88)
    _company_job(con, "u2", "Müller Software GmbH", 70)
    con.commit()

    view = jobs._load_jobs("new", jobs.PILE_NONE, 0)
    assert view["total"] == 1
    assert view["rows"][0]["id"] == best
    assert view["rows"][0]["company_count"] == 2

    # and the claim is true: the gate really does treat them as one company
    from jobdeck import db
    db.add_bewerbung(con, {"gesendet_am": "2026-08-01",
                           "firma": "MÜLLER Software GmbH", "email": "",
                           "kanal": "E-Mail", "status": "Gesendet"})
    assert find_duplicate_bewerbung(con, "Müller Software GmbH", "") is not None


def test_a_company_literally_named_like_a_blank_key_stays_its_own_group(con,
                                                                       data_dir):
    blank = _company_job(con, "k1", "", 80)
    named = _company_job(con, "k2", f"job:{blank}", 70)
    con.commit()
    view = jobs._load_jobs("new", jobs.PILE_NONE, 0)
    assert view["total"] == 2
    assert [r["id"] for r in view["rows"]] == [blank, named]
    assert [r["company_count"] for r in view["rows"]] == [1, 1]


def test_grouped_paging_walks_companies_without_skipping_or_repeating(con, data_dir):
    """Grouped mode has its own query, so it needs its own paging test: the flat
    tests above cannot see an OFFSET missing from list_job_groups."""
    for n in range(120):
        _company_job(con, f"gp{n}", f"Firma {n:03d}", n + 1)
        _company_job(con, f"gp{n}b", f"Firma {n:03d}", 1)   # a sibling each
    con.commit()

    seen, page = [], 0
    while True:
        view = jobs._load_jobs("new", jobs.PILE_NONE, page)
        assert view["total"] == 120                 # companies, not the 240 rows
        seen += [r["company"] for r in view["rows"]]
        if page + 1 >= view["pages"]:
            break
        page += 1
    assert page == 2 and view["pages"] == 3
    assert len(seen) == 120 and len(set(seen)) == 120   # no repeat, none skipped
    # and the page really is a slice of the ordering, not the head of it twice
    first = jobs._load_jobs("new", jobs.PILE_NONE, 0)["rows"]
    second = jobs._load_jobs("new", jobs.PILE_NONE, 1)["rows"]
    assert first[0]["match_score"] == 120 and second[0]["match_score"] == 70


def test_a_hidden_pile_never_leaks_in_as_a_sibling(con, data_dir):
    """The sibling query is a THIRD consumer of the filters. If it ignored them,
    the rows the working list hides would reappear underneath a company."""
    keep = _company_job(con, "s1", "Firma", 90)
    visible_sibling = _company_job(con, "s2", "Firma", 80)
    mismatch = _company_job(con, "s3", "Firma", 0)
    dead = _company_job(con, "s4", "Firma", 85)
    con.execute("UPDATE jobs SET liveness='gone' WHERE id=?", (dead,))
    con.commit()

    view = jobs._load_jobs("new", jobs.PILE_NONE, 0)
    head = view["rows"][0]
    assert head["id"] == keep
    assert head["company_count"] == 2               # not 4
    siblings = view["siblings"][head["company_key"]]
    assert [r["id"] for r in siblings] == [visible_sibling]
    assert mismatch not in [r["id"] for r in siblings]
    assert dead not in [r["id"] for r in siblings]

    # each hidden row is still reachable from its own pile, with its siblings
    dead_view = jobs._load_jobs("new", jobs.PILE_DEAD, 0)
    assert [r["id"] for r in dead_view["rows"]] == [dead]


def test_no_handler_writes_another_control_on_the_server():
    """Mutual exclusion between two switches means the handler writes the OTHER
    switch — and NiceGUI fires that server-side write as a background task, so
    two clicks read in one socket turn make the two echo each other into an
    endless refresh loop. The page uses one value for the three views instead;
    this pins that nothing reintroduces a cross-write."""
    source = pathlib.Path(jobs.__file__).read_text()
    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            # `element.value = x` on anything that is not our own state dict
            if (isinstance(target, ast.Attribute) and target.attr == "value"
                    and not isinstance(target.value, ast.Subscript)):
                offenders.append(f"{jobs.__name__}:{node.lineno}")
    assert offenders == [], (
        f"a server-side control write at {offenders}: NiceGUI dispatches it as a "
        f"background task, which is how two switches echo each other forever"
    )


@pytest.mark.parametrize("collapse", [True, False])
@pytest.mark.parametrize("pile", ["", "mismatches", "dead"])
def test_the_printed_total_always_matches_the_rows_it_describes(con, data_dir,
                                                               pile, collapse):
    """The count and the list are two queries. If they ever filter differently
    the header lies about the page beneath it, in either grouping mode and in
    every view."""
    from jobdeck import db
    for n in range(7):
        _company_job(con, f"c{n}", f"Firma {n % 3}", 90 - n)
    mismatch = _company_job(con, "m", "Firma 9", 0)
    dead = _company_job(con, "d", "Firma 8", 70)
    con.execute("UPDATE jobs SET liveness='gone' WHERE id=?", (dead,))
    con.commit()
    assert mismatch and dead

    view = jobs._load_jobs("new", pile, 0, collapse=collapse)
    assert len(view["rows"]) == view["total"], (pile, collapse)
    # and the count helper agrees with the listing helper it is paired with
    filters = jobs._view_filters(pile, "new")
    count = db.count_job_groups if collapse else db.count_jobs
    listing = db.list_job_groups if collapse else db.list_jobs
    assert count(con, "new", **filters) == len(
        listing(con, "new", limit=500, **filters))


def test_changing_the_view_always_returns_to_the_first_page(con, data_dir):
    """Page 3 of a different list means nothing — and an offset past the end
    would render an empty page until the user noticed."""
    source = pathlib.Path(jobs.__file__).read_text()
    for handler in ("async def set_filter", "async def set_pile",
                    "async def set_collapse"):
        body = source[source.index(handler):]
        body = body[:body.index("await refresh()")]
        assert 'page["value"] = 0' in body, f"{handler} does not reset the page"

    # and the loader is what makes a stale offset harmless either way
    for n in range(120):
        _company_job(con, f"p{n}", f"Firma {n:03d}", n + 1)
    con.commit()
    assert jobs._load_jobs("new", jobs.PILE_NONE, 99)["page"] == 2
    assert jobs._load_jobs("new", jobs.PILE_NONE, -5)["page"] == 0


# --------------------------------------------------------------------------
# A draft being written was invisible everywhere for the minute it took: the
# row existed and no view listed it, so pressing Draft twice was the only way
# to learn the app was working.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("status, expected_in_line", [
    ("generating", "wird gerade geschrieben"),
    ("ready", "Review queue"),
    ("approved", "Review queue"),
    ("sending", "Versand"),
    ("failed", "fehlgeschlagen"),
    ("sent", "gesendet"),
])
def test_the_row_says_what_its_draft_is_doing(status, expected_in_line):
    text, classes = jobs._draft_line(status)
    assert expected_in_line in text
    assert classes.startswith("text-sm ")


@pytest.mark.parametrize("status", ["discarded", "", None])
def test_a_posting_with_nothing_to_report_says_nothing(status):
    """A discarded draft leaves the posting where it started; a line about a
    draft that no longer exists would only be in the way."""
    assert jobs._draft_line(status) == ("", "")


def test_the_inbox_carries_the_draft_state_in_every_view(con, data_dir):
    from jobdeck import db
    quiet = _company_job(con, "d1", "Ohne Entwurf GmbH", 90)
    busy = _company_job(con, "d2", "Mit Entwurf GmbH", 80)
    db.upsert_draft(con, busy, {"status": "generating"})
    con.commit()

    for collapse in (True, False):
        rows = {r["id"]: r["draft_status"]
                for r in jobs._load_jobs("new", jobs.PILE_NONE, 0,
                                         collapse=collapse)["rows"]}
        assert rows[busy] == "generating", collapse
        assert rows[quiet] is None, collapse


def test_a_sibling_row_carries_it_too(con, data_dir):
    """Siblings come from the shared ranking CTE — if the column were added to
    the flat query only, a grouped company's other postings would lose it."""
    from jobdeck import db
    best = _company_job(con, "s1", "Eine Firma GmbH", 90)
    second = _company_job(con, "s2", "Eine Firma GmbH", 70)
    db.upsert_draft(con, second, {"status": "ready"})
    con.commit()
    view = jobs._load_jobs("new", jobs.PILE_NONE, 0)
    head = view["rows"][0]
    assert head["id"] == best
    siblings = view["siblings"][head["company_key"]]
    assert [(r["id"], r["draft_status"]) for r in siblings] == [(second, "ready")]


def test_the_row_describes_the_same_draft_every_button_acts_on(con, data_dir):
    """drafts.job_id has no UNIQUE constraint and get_draft_by_job answers with
    the NEWEST row. A view reading any other one would describe a draft the
    buttons do not touch."""
    from jobdeck import db
    job_id = _company_job(con, "n1", "Zwei Entwürfe GmbH", 90)
    con.execute(
        "INSERT INTO drafts (job_id, status, created_at, updated_at) "
        "VALUES (?, 'discarded', '2026-08-01T10:00:00', '2026-08-01T10:00:00')",
        (job_id,))
    con.execute(
        "INSERT INTO drafts (job_id, status, created_at, updated_at) "
        "VALUES (?, 'ready', '2026-08-02T10:00:00', '2026-08-02T10:00:00')",
        (job_id,))
    con.commit()
    assert db.get_draft_by_job(con, job_id)["status"] == "ready"
    view = jobs._load_jobs("new", jobs.PILE_NONE, 0)
    assert view["rows"][0]["draft_status"] == "ready"


def test_the_draft_button_is_guarded_on_a_live_claim_not_on_a_status():
    """A row can say 'generating' long after the process holding the claim
    died, and this button is the only thing that can restart it — see
    tests/test_draft_visibility_pages.py for the rendered proof."""
    source = pathlib.Path(jobs.__file__).read_text()
    guard = source[:source.index('"Draft application"')]
    guard = guard[guard.rindex('if (job["status"]'):]
    assert "_claim_is_running(job)" in guard
    # and the outcome reaches the row: without a refresh the line would still
    # claim the draft is being written after it finished
    handler = source[source.index("async def draft("):]
    handler = handler[:handler.index("async def resolve_channel")]
    assert "await refresh()" in handler


@pytest.mark.parametrize("age, running", [
    (0.0, True), (1.0, True),
    (drafting_module().CLAIM_TIMEOUT_MIN - 0.1, True),
    (drafting_module().CLAIM_TIMEOUT_MIN + 0.1, False),
    (600.0, False),
])
def test_only_a_claim_the_reclaim_would_refuse_hides_the_button(age, running):
    """The button must come back at exactly the moment drafting._claim would
    take the row over — one minute earlier and a second draft is paid for,
    one minute later and an abandoned draft is unrecoverable."""
    import datetime
    stamp = (datetime.datetime.now()
             - datetime.timedelta(minutes=age)).isoformat(timespec="seconds")
    job = {"draft_status": "generating", "draft_updated_at": stamp}
    assert jobs._claim_is_running(job) is running
    assert ("abgebrochen" in jobs._draft_line("generating", stamp)[0]) is not running


def test_an_unreadable_claim_timestamp_reads_as_running():
    """A stored value we cannot parse is not evidence the process died — and
    treating it as dead would let a second claim start while the first is
    still spending money."""
    assert jobs._claim_is_running(
        {"draft_status": "generating", "draft_updated_at": "not a timestamp"})


_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _own_statements(func):
    """Nodes belonging to this function, not to a scope nested inside it.

    The whole subtree of a nested def is skipped, not merely its header — and
    a lambda counts as nested too: `on_click=lambda: ui.navigate.to(...)` runs
    when the button is clicked, in that button's own live slot, not while the
    page around it is being built."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, _NESTED_SCOPES):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _unhosted_ui_calls(path):
    """Every element a page builds on a slot a refresh may already have taken
    away: a bare `ui.notify` anywhere (the page has `say` for that), and a
    `ui.dialog()` or `ui.navigate.to()` reached AFTER the handler has awaited.

    Before an await the sender's slot is alive by definition, so a synchronous
    click handler navigating straight away is fine."""
    tree = ast.parse(pathlib.Path(path).read_text())
    page = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name.endswith("_page"))
    hosted = {
        node.lineno
        for stmt in ast.walk(page) if isinstance(stmt, ast.With)
        and any(ast.unparse(i.context_expr) == "overlay" for i in stmt.items)
        for node in ast.walk(stmt) if hasattr(node, "lineno")
    }
    offenders = []
    # Sync helpers count too: `say` is a plain def, and dropping its
    # `with overlay:` restores the whole defect while every async handler
    # still looks innocent.
    scopes = [page] + [n for n in ast.walk(page)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for func in scopes:
        own = list(_own_statements(func))
        awaits = [n.lineno for n in own if isinstance(n, ast.Await)]
        first_await = min(awaits, default=None)
        for node in own:
            if not isinstance(node, ast.Call):
                continue
            target = ast.unparse(node.func)
            if target == "ui.notify" and node.lineno not in hosted:
                offenders.append((func.name, target, node.lineno))
            elif (target in ("ui.dialog", "ui.navigate.to")
                    and first_await is not None and node.lineno > first_await
                    and node.lineno not in hosted):
                offenders.append((func.name, target, node.lineno))
    return sorted(set(offenders))


@pytest.mark.parametrize("page_module", ["jobs.py", "queue.py"])
def test_nothing_is_shown_on_a_slot_that_may_already_be_gone(page_module):
    """The defect CLASS, not the one place it was found. A handler runs in the
    slot of the element that fired it; any refresh — its own, a concurrent
    one, or the queue's timer — deletes that slot while the handler is
    awaiting. NiceGUI then raises from context.client and handle_event
    swallows it, so the user sees NOTHING: the drafting error notification
    vanished exactly this way with the whole suite green."""
    path = pathlib.Path(jobs.__file__).parent / page_module
    offenders = _unhosted_ui_calls(path)
    assert offenders == [], (
        f"{page_module}: built where a refresh can delete it — "
        + ", ".join(f"{fn}() {what} at line {line}" for fn, what, line in offenders)
        + " (use say(...) for messages, `with overlay:` for the rest)")


@pytest.mark.parametrize("page_module", ["jobs.py", "queue.py"])
def test_the_slot_rule_is_actually_binding(page_module):
    """A scan that finds no candidates would pass on any code at all."""
    source = (pathlib.Path(jobs.__file__).parent / page_module).read_text()
    assert source.count("with overlay") >= 2, "nothing is hosted"
    assert "def say(" in source, "the page has no safe way to notify"
    assert source.count("say(") >= 5, "messages are not routed through it"


def test_nothing_the_user_sees_is_built_on_a_slot_a_refresh_just_deleted():
    """A handler runs in the slot of the button that fired it, and refresh()
    clears the container that button lives in. NiceGUI then raises
    'The parent element this slot belongs to has been deleted' from
    context.client — so the failure notification never appears and the error
    it was reporting is swallowed. Caught in the running app 2026-08-10, with
    the whole suite green: an AST rule is what makes it stay caught."""
    tree = ast.parse(pathlib.Path(jobs.__file__).read_text())
    handlers = [n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "draft"]
    assert len(handlers) == 1, "the draft handler moved or was renamed"

    # The name alone proves nothing: `overlay = container` would satisfy every
    # rule below while restoring the defect exactly.
    bindings = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and [ast.unparse(t) for t in n.targets] == ["overlay"]]
    assert len(bindings) == 1 and ast.unparse(
        bindings[0].value).startswith("ui.column()"), (
        "`overlay` must be its own element, not another name for one a "
        "refresh clears")

    def refreshes(node) -> bool:
        return any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "refresh"
                   for c in ast.walk(node))

    body = handlers[0].body
    after = body[next(i for i, s in enumerate(body) if refreshes(s)) + 1:]
    assert after, "the handler does nothing after refreshing — the guard is moot"

    def is_overlay_reset(node) -> bool:
        """`overlay.clear()` builds nothing, so it needs no live parent."""
        return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == "overlay.clear")

    hosted = 0
    for statement in after:
        if is_overlay_reset(statement):
            continue
        assert isinstance(statement, ast.With), (
            f"line {statement.lineno}: built outside a live parent after "
            "refresh() deleted this handler's slot")
        assert [ast.unparse(i.context_expr) for i in statement.items] == ["overlay"], (
            f"line {statement.lineno}: the host must be the sibling `overlay`, "
            "not an element the refresh can delete")
        hosted += 1
    assert hosted, "nothing is hosted — the rule would pass on an empty tail"


def test_the_sql_filter_agrees_with_the_gate_on_every_shape(con, data_dir):
    """`db.APPLIED_FIRM_SQL` is a SECOND implementation of _first_match — it has
    to run where the paging and the counts do. Two hand-written copies of one
    rule drift, and this one decides whether a posting is shown at all, so the
    two are pinned equal over a generated corpus rather than argued about."""
    from jobdeck import db
    from jobdeck.dedupe import _BEWERBUNGEN_SQL, _first_match

    firms = ["Müller GmbH", "MÜLLER  GmbH", "müller gmbh ", "Müller AG",
             "a.b® GmbH", "a.b GmbH", "ab GmbH", "180° GmbH", "180 GmbH",
             "ACME™", "ACME", "", "   ", "Ⓐ GmbH", "Ⓑ GmbH", "Beispiel\xadGmbH"]
    mails = ["", "jobs@mueller.de", "JOBS@MUELLER.DE", "  ",
             "bewerbung^x@a.de", "bewerbungx@a.de", "hr@andere.de"]
    for firma, email in [("Müller GmbH", "jobs@mueller.de"), ("ACME AG", ""),
                         ("", "hr@andere.de"), ("  ", "  ")]:
        db.add_bewerbung(con, {"gesendet_am": "2026-06-10", "firma": firma,
                               "email": email, "kanal": "E-Mail",
                               "status": "Gesendet"})
    for index, (firma, email) in enumerate(
            [(f, m) for f in firms for m in mails]):
        db.insert_job_if_new(con, {
            "source": "stub", "external_id": f"x{index}", "title": "Dev",
            "company": firma, "contact_email": email,
            "url": f"https://e.example/{index}"})
    con.commit()

    rows = con.execute(_BEWERBUNGEN_SQL).fetchall()
    by_sql = {r[0] for r in con.execute(
        f"SELECT id FROM jobs WHERE {db.APPLIED_FIRM_SQL}")}
    by_gate = {j["id"] for j in con.execute("SELECT * FROM jobs")
               if _first_match(rows, j["company"], j["contact_email"])}
    assert by_sql == by_gate, (
        f"SQL-only: {sorted(by_sql - by_gate)}, gate-only: {sorted(by_gate - by_sql)}")
    assert by_gate, "the corpus produced no matches — the check would be vacuous"
    assert by_gate != {j[0] for j in con.execute("SELECT id FROM jobs")}, \
        "everything matched — the check would pass on a filter that never hides"
