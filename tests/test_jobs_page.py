"""Data-layer behaviour of the job inbox page (no NiceGUI rendering)."""

import ast
import inspect
import pathlib
import time
from html.parser import HTMLParser

import pytest
from nicegui import ui
from nicegui.elements.markdown import prepare_content

from jobdeck import freshness
from jobdeck.ui import helpers
from jobdeck.ui.helpers import openable_url, posting_markdown
from jobdeck.ui.pages import jobs


def _held(sent_on="2026-06-12", reopens_on="2026-08-11", position=""):
    """The decision the screens now receive: a company inside its window."""
    from jobdeck import identity

    return identity.Decision(
        verdict=identity.COOLING_OFF, application_id=1, position=position,
        sent_on=sent_on, reopens_on=reopens_on,
    )



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


def test_every_named_view_is_one_coherent_way_of_looking():
    """One list of views replaced six status filters and four pile switches,
    which could be combined into states describing nothing ("applied postings,
    mismatches only"). Each view therefore opens at most ONE pile, and the two
    working views open none."""
    working = [v for v in jobs.VIEWS if v.key in ("neu", "offen")]
    assert [v.key for v in working] == ["neu", "offen"]
    for view in working:
        assert all(view.filters[arm] == "exclude"
                   for arm in ("mismatches", "gone", "applied", "old"))
    for view in jobs.VIEWS:
        opened = [arm for arm, value in view.filters.items() if value == "only"]
        assert len(opened) <= 1, f"{view.key} opens {opened}"
    assert len({v.key for v in jobs.VIEWS}) == len(jobs.VIEWS), "duplicate key"
    assert all(v.empty for v in jobs.VIEWS), "a view with nothing in it says nothing"


def test_the_new_view_is_the_working_list_minus_what_he_has_read():
    neu, offen = jobs.view_for("neu"), jobs.view_for("offen")
    assert neu.filters["opened"] == "exclude"
    assert "opened" not in offen.filters
    assert {k: v for k, v in neu.filters.items() if k != "opened"} == offen.filters


def test_an_unknown_view_falls_back_rather_than_raising():
    """The key comes from a control and one day from a URL; an unknown one must
    not be a screen he cannot open."""
    assert jobs.view_for("erfunden").key == jobs.DEFAULT_VIEW
    assert jobs.view_for("").key == jobs.DEFAULT_VIEW


_COUNTS = {"mismatches": 129, "dead": 58, "applied_firm": 30, "old": 12}


@pytest.mark.parametrize("view_key, counts, expected", [
    ("offen", _COUNTS,
     "129 passen nicht · 58 offline · "
     "30 bei zurückgestellten Firmen · 12 älter als 45 Tage"),
    # …and every one of those four can be ONE
    ("offen", {"mismatches": 1, "dead": 1, "applied_firm": 1, "old": 1},
     "1 passt nicht · 1 offline · 1 bei einer zurückgestellten Firma · "
     "1 älter als 45 Tage"),
    ("offen", {**_COUNTS, "mismatches": 0, "applied_firm": 0, "old": 0},
     "58 offline"),
    ("offen", {"mismatches": 0, "dead": 0, "applied_firm": 0, "old": 0}, ""),
    # a pile view INCLUDES the other piles, so it must not claim to hide them —
    # the label is derived from the filters the query really used
    ("firma_kontaktiert", _COUNTS,
     "30 Stellen bei Firmen, die gerade zurückgestellt sind"),
    ("passt_nicht", _COUNTS, "129 Anzeigen verletzen eine harte Anforderung"),
    ("offline", _COUNTS, "58 Anzeigen sind offline"),
    ("alt", _COUNTS, "12 Anzeigen älter als 45 Tage"),
    # every one of these figures can be ONE — this project has shipped
    # "1 Bewerbungen" before
    ("offline", {**_COUNTS, "dead": 1}, "1 Anzeige ist offline"),
    ("alt", {**_COUNTS, "old": 1}, "1 Anzeige älter als 45 Tage"),
    ("passt_nicht", {**_COUNTS, "mismatches": 1},
     "1 Anzeige verletzt eine harte Anforderung"),
    ("firma_kontaktiert", {**_COUNTS, "applied_firm": 1},
     "1 Stelle bei einer Firma, die gerade zurückgestellt ist"),
    # a view of what he set aside himself hides nothing at all
    ("vorgemerkt", _COUNTS, ""),
    ("in_arbeit", _COUNTS, ""),
])
def test_the_hidden_line_can_never_contradict_the_list(view_key, counts, expected):
    # never a total either: a posting can be both a mismatch and offline, so
    # adding the two would double-count it
    assert jobs._hidden_line(jobs.view_for(view_key), counts, 45) == expected


def test_every_pile_the_line_names_is_a_door_to_the_view_that_holds_it():
    """The counts were printed and never clickable, so a pile was named on
    screen and reachable only through a dropdown that named it again. A part
    with no view is one that describes the CURRENT list rather than another."""
    parts = jobs.hidden_parts(jobs.view_for("offen"), _COUNTS, 45)
    assert [p["view"] for p in parts] == \
        ["passt_nicht", "offline", "firma_kontaktiert", "alt"]
    for part in parts:
        assert jobs.view_for(part["view"]).key == part["view"], \
            f"{part['text']} points at a view that does not exist"

    # a pile view states what it IS showing, and that is not a door
    inside = jobs.hidden_parts(jobs.view_for("passt_nicht"), _COUNTS, 45)
    assert [p["view"] for p in inside] == [None]


def test_a_posting_he_has_acted_on_is_never_hidden_from_its_own_view(con, data_dir):
    """The row of a posting whose form he opened carries the button that
    finishes the job ("I applied — record it"). Hiding it for being offline
    would hide that."""
    from jobdeck import db
    job_id = _company_job(con, "p1", "Firma", 90)
    con.execute("UPDATE jobs SET liveness='gone' WHERE id=?", (job_id,))
    db.mark_form_opened(con, job_id)
    con.commit()
    portal = jobs._load_jobs("in_arbeit", 0)
    assert [r["id"] for r in portal["rows"]] == [job_id]
    assert portal["view"].key == "in_arbeit"
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
        view = jobs._load_jobs("offen", page)
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
    view = jobs._load_jobs("offen", 99)
    assert view["page"] == 1 and len(view["rows"]) == 10
    empty = jobs._load_jobs("beworben", 99)
    assert empty["page"] == 0 and empty["rows"] == [] and empty["total"] == 0


def test_paging_does_not_skip_or_repeat_a_row_at_the_boundary(con, data_dir):
    _seed_scored(con, 51)
    first = jobs._load_jobs("offen", 0)
    second = jobs._load_jobs("offen", 1)
    assert len(first["rows"]) == 50 and len(second["rows"]) == 1
    assert set(r["id"] for r in first["rows"]).isdisjoint(
        r["id"] for r in second["rows"])
    # best score first across the page break, not only inside a page
    assert first["rows"][0]["match_score"] == 51
    assert second["rows"][0]["match_score"] == 1


@pytest.mark.parametrize("page, total, shown, expected", [
    (0, 266, 50, "1–50 von 266 Firmen"),
    (1, 266, 50, "51–100 von 266 Firmen"),
    (5, 287, 37, "251–287 von 287 Firmen"),
    (0, 3, 3, "1–3 von 3 Firmen"),
    (0, 0, 0, ""),
])
def test_the_range_line_names_the_unit_it_counts(page, total, shown, expected):
    # a row is a COMPANY while the pile counts beside it are POSTINGS; an
    # unlabelled pair of numbers invites comparing them
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

    view = jobs._load_jobs("offen", 0)
    assert view["total"] == 2                     # companies, not postings
    assert [r["id"] for r in view["rows"]] == [best, other]
    head = view["rows"][0]
    assert head["company_count"] == 3
    siblings = view["siblings"][head["company_key"]]
    assert [r["match_score"] for r in siblings] == [70, 60]   # best-ranked first



def test_a_blank_company_never_groups_with_another(con, data_dir):
    # an empty employer field is missing data, not a company they share
    first = _company_job(con, "x1", "", 80)
    second = _company_job(con, "x2", "   ", 70)
    con.commit()
    view = jobs._load_jobs("offen", 0)
    assert view["total"] == 2
    assert [r["company_count"] for r in view["rows"]] == [1, 1]
    assert [r["id"] for r in view["rows"]] == [first, second]


def test_the_group_that_represents_a_company_is_chosen_by_the_aged_score(
    con, data_dir
):
    import datetime
    today = datetime.date.today()
    # 40 days: aged enough to lose 12 points, still inside the working list
    # (past the age threshold it would be in the "Alte Anzeigen" pile instead)
    stale_star = _company_job(con, "s1", "Firma", 85,
                              (today - datetime.timedelta(days=40)).isoformat())
    fresh_good = _company_job(con, "s2", "Firma", 78,
                              (today - datetime.timedelta(days=1)).isoformat())
    con.commit()
    view = jobs._load_jobs("offen", 0)
    # 85 aged to 73 loses to a fresh 78: the row that represents the company is
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

    view = jobs._load_jobs("offen", 0)
    # the 90 is offline and the 0 violates a hard requirement: neither may
    # represent the company, and neither may be counted as one of its postings
    assert [r["id"] for r in view["rows"]] == [keep]
    assert view["rows"][0]["company_count"] == 1
    assert view["siblings"] == {}

    # each pile stays reachable as its own grouped view
    assert [r["id"] for r in jobs._load_jobs("offline", 0)["rows"]] \
        == [dead]
    assert [r["id"] for r in
            jobs._load_jobs("passt_nicht", 0)["rows"]] == [mismatch]
    assert db.count_job_groups(con, "new") == 1


def test_one_employer_cannot_decide_how_much_a_page_renders(con, data_dir):
    from jobdeck import db
    for n in range(30):
        _company_job(con, f"m{n}", "Massenposter GmbH", 90 - n)
    con.commit()
    view = jobs._load_jobs("offen", 0)
    assert view["total"] == 1
    head = view["rows"][0]
    assert head["company_count"] == 30            # the truth is still reported
    siblings = view["siblings"][head["company_key"]]
    assert len(siblings) == db.SIBLINGS_PER_COMPANY   # but the render is bounded
    assert [r["match_score"] for r in siblings] == list(range(89, 79, -1))


def test_a_view_that_stands_on_no_status_obeys_the_chosen_order(con, data_dir):
    """Vorgemerkt and In Arbeit stand on no status, and they used to fall back
    to insertion order — which made the sort control a lie in two of the eleven
    views. The order the caller asked for now applies in every one of them.

    The dates DISAGREE with both the ids and the scores on purpose, so neither
    an id ordering nor a score ordering can pass by accident."""
    import datetime

    from jobdeck import db
    day = datetime.date.today()
    old_best = _company_job(con, "z1", "Alpha", 95,
                            (day - datetime.timedelta(days=20)).isoformat())
    weak_new = _company_job(con, "z2", "Beta", 10,
                            (day - datetime.timedelta(days=1)).isoformat())
    middling = _company_job(con, "z3", "Gamma", 50,
                            (day - datetime.timedelta(days=10)).isoformat())
    for job_id in (old_best, weak_new, middling):
        db.set_bookmark(con, job_id, True)
    con.commit()

    by_date = db.list_jobs(con, bookmarked="only", sort="date")
    by_score = db.list_jobs(con, bookmarked="only", sort="score")

    assert [r["id"] for r in by_date] == [weak_new, middling, old_best]
    assert [r["id"] for r in by_score] == [old_best, middling, weak_new]


def test_companies_group_the_way_the_duplicate_gate_compares_them(con, data_dir):
    """dedupe.py exists because SQLite's lower() folds ASCII only. The grouped
    view currently mirrors the legacy gate, so it must use the same comparison
    function. ADR 0002 defines the narrower target identity policy."""
    from jobdeck.dedupe import find_duplicate_bewerbung
    best = _company_job(con, "u1", "MÜLLER Software GmbH", 88)
    _company_job(con, "u2", "Müller Software GmbH", 70)
    con.commit()

    view = jobs._load_jobs("offen", 0)
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
    view = jobs._load_jobs("offen", 0)
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
        view = jobs._load_jobs("offen", page)
        assert view["total"] == 120                 # companies, not the 240 rows
        seen += [r["company"] for r in view["rows"]]
        if page + 1 >= view["pages"]:
            break
        page += 1
    assert page == 2 and view["pages"] == 3
    assert len(seen) == 120 and len(set(seen)) == 120   # no repeat, none skipped
    # and the page really is a slice of the ordering, not the head of it twice
    first = jobs._load_jobs("offen", 0)["rows"]
    second = jobs._load_jobs("offen", 1)["rows"]
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

    view = jobs._load_jobs("offen", 0)
    head = view["rows"][0]
    assert head["id"] == keep
    assert head["company_count"] == 2               # not 4
    siblings = view["siblings"][head["company_key"]]
    assert [r["id"] for r in siblings] == [visible_sibling]
    assert mismatch not in [r["id"] for r in siblings]
    assert dead not in [r["id"] for r in siblings]

    # each hidden row is still reachable from its own pile, with its siblings
    dead_view = jobs._load_jobs("offline", 0)
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


@pytest.mark.parametrize("view_key", [v.key for v in jobs.VIEWS])
def test_the_printed_total_always_matches_the_rows_it_describes(con, data_dir,
                                                                view_key):
    """The count and the list are two queries. If they ever filter differently
    the header lies about the page beneath it — in every named view."""
    from jobdeck import db
    for n in range(7):
        _company_job(con, f"c{n}", f"Firma {n % 3}", 90 - n)
    mismatch = _company_job(con, "m", "Firma 9", 0)
    dead = _company_job(con, "d", "Firma 8", 70)
    con.execute("UPDATE jobs SET liveness='gone' WHERE id=?", (dead,))
    con.commit()
    assert mismatch and dead

    view = jobs._load_jobs(view_key, 0)
    assert len(view["rows"]) == view["total"], view_key
    # and the count helper agrees with the listing helper it is paired with
    named = jobs.view_for(view_key)
    filters = {**named.filters, "stale_age_days": view["stale_age_days"]}
    assert db.count_job_groups(con, named.status, **filters) == len(
        db.list_job_groups(con, named.status, limit=500, **filters))


def test_changing_the_view_always_returns_to_the_first_page(con, data_dir):
    """Page 3 of a different list means nothing — and an offset past the end
    would render an empty page until the user noticed."""
    source = pathlib.Path(jobs.__file__).read_text()
    for handler in ("async def set_view", "async def set_search"):
        body = source[source.index(handler):]
        body = body[:body.index("await refresh(")]
        assert 'state["page"] = 0' in body, f"{handler} does not reset the page"

    # and the loader is what makes a stale offset harmless either way
    for n in range(120):
        _company_job(con, f"p{n}", f"Firma {n:03d}", n + 1)
    con.commit()
    assert jobs._load_jobs("offen", 99)["page"] == 2
    assert jobs._load_jobs("offen", -5)["page"] == 0


# --------------------------------------------------------------------------
# A draft being written was invisible everywhere for the minute it took: the
# row existed and no view listed it, so pressing Draft twice was the only way
# to learn the app was working.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("status, expected_in_line", [
    ("generating", "wird gerade geschrieben"),
    ("ready", "Postausgang"),
    ("approved", "Postausgang"),
    ("sending", "Versand"),
    ("failed", "fehlgeschlagen"),
    ("sent", "gesendet"),
    ("filed", "eingereicht"),
])
def test_the_row_says_what_its_draft_is_doing(status, expected_in_line):
    text, classes = jobs._draft_line(status)
    assert expected_in_line in text
    assert classes.startswith("text-sm ")


def test_a_letter_with_an_employer_is_never_offered_for_rewriting():
    """'filed' joins 'sent': both mean an employer holds the letter, so
    offering "Anschreiben neu schreiben" would offer to rewrite the record of
    what went out. The row said nothing at all for a filed one."""
    from jobdeck.constants import DRAFT_DELIVERED
    for status in DRAFT_DELIVERED:
        steps = {s.key: s for s in jobs.apply_steps(
            _row(apply_channel="direct_email", contact_email="hr@x.de",
                 draft_status=status))}
        assert steps[jobs.STEP_DRAFT].enabled is False, status


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
                for r in jobs._load_jobs("offen", 0)["rows"]}
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
    view = jobs._load_jobs("offen", 0)
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
    view = jobs._load_jobs("offen", 0)
    assert view["rows"][0]["draft_status"] == "ready"


def test_the_main_button_is_guarded_on_a_live_claim_not_on_a_status():
    """A row can say 'generating' long after the process holding the claim
    died, and the main button is the only thing that can restart it — hiding it
    for as long as the STATUS says 'generating' left a posting that could never
    be drafted again. The outcome must also reach the screen: without a refresh
    the reader would still claim the draft is being written after it finished."""
    source = pathlib.Path(jobs.__file__).read_text()
    handler = source[source.index("async def draft("):]
    handler = handler[:handler.index("async def resolve_channel")]
    assert "await refresh(force=True)" in handler, (
        "a plain refresh() can be skipped when the data did not change — and "
        "the pressed button stays relabelled 'wird geschrieben …' forever")


@pytest.mark.parametrize("age, running", [
    (0.0, True), (1.0, True),
    (drafting_module().CLAIM_TIMEOUT_MIN - 0.1, True),
    (drafting_module().CLAIM_TIMEOUT_MIN + 0.1, False),
    (600.0, False),
])
def test_only_a_claim_the_reclaim_would_refuse_blocks_the_button(age, running):
    """The button must come back at exactly the moment drafting._claim would
    take the row over — one minute earlier and a second draft is paid for,
    one minute later and an abandoned draft is unrecoverable."""
    import datetime
    stamp = (datetime.datetime.now()
             - datetime.timedelta(minutes=age)).isoformat(timespec="seconds")
    steps = {s.key: s for s in jobs.apply_steps(
        _row(draft_status="generating", draft_updated_at=stamp,
             apply_channel="direct_email", contact_email="hr@x.de"))}
    assert steps[jobs.STEP_DRAFT].enabled is not running
    assert ("abgebrochen" in jobs._draft_line("generating", stamp)[0]) is not running


def test_an_unreadable_claim_timestamp_reads_as_running():
    """A stored value we cannot parse is not evidence the process died — and
    treating it as dead would let a second claim start while the first is
    still spending money."""
    steps = {s.key: s for s in jobs.apply_steps(
        _row(draft_status="generating", draft_updated_at="not a timestamp",
             apply_channel="direct_email", contact_email="hr@x.de"))}
    assert steps[jobs.STEP_DRAFT].enabled is False


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
    # The whole module, not just the @ui.page function: the cockpit builds its
    # rows from a module-level `_render`, and a rule that only looked inside the
    # page function would pass it without reading a line of it.
    hosted = {
        node.lineno
        for stmt in ast.walk(tree) if isinstance(stmt, ast.With)
        and any(ast.unparse(i.context_expr) == "overlay" for i in stmt.items)
        for node in ast.walk(stmt) if hasattr(node, "lineno")
    }
    offenders = []
    # Sync helpers count too: `say` is a plain def, and dropping its
    # `with overlay:` restores the whole defect while every async handler
    # still looks innocent.
    scopes = [n for n in ast.walk(tree)
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


def _overlay_files() -> list[str]:
    """Every module that CLEARS a container and rebuilds it — DERIVED.

    That is the rule's own premise, so it is read out of the source rather
    than out of a hand-written list. The list silently skipped whichever page
    was newest — the same shape as the signature rule that matched a spelling
    and covered nothing for a whole slice — and it carried an excuse for a
    page (`applications.py`) that no longer exists, while the screen that
    replaced it clears three containers on a timer.

    A module that clears nothing is excluded because no handler's slot can die
    under it, which is exactly why `settings.py` is not in the result.
    """
    pages = pathlib.Path(jobs.__file__).parent
    candidates = [(path.name, path) for path in pages.glob("*.py")] + [
        (f"../{path.name}", path) for path in pages.parent.glob("*.py")]
    found = []
    for name, path in candidates:
        if path.stem in ("__init__", "app"):
            continue
        tree = ast.parse(path.read_text())
        clears = any(isinstance(node, ast.Call)
                     and ast.unparse(node.func).endswith(".clear")
                     and not node.args
                     for node in ast.walk(tree))
        if clears:
            found.append(name)
    return sorted(found)


# Every page that CLEARS a container and rebuilds it — DERIVED, like the
# overlay rule below. The hand-written list carried an excuse for
# `applications.py` ("its refresh assigns table.rows and deletes no element"),
# and that excuse outlived the page: `bewerbungen.py` replaced it and clears
# three containers on a timer, so the list would have skipped the very screen
# most exposed to this.
@pytest.mark.parametrize("page_module", _overlay_files())
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


@pytest.mark.parametrize("page_module",
                         ["jobs.py", "queue.py", "unterlagen.py",
                          "../draft_editor.py"])
def test_the_slot_rule_is_actually_binding(page_module):
    """A scan that finds no candidates would pass on any code at all."""
    source = (pathlib.Path(jobs.__file__).parent / page_module).read_text()
    assert source.count("with overlay") >= 2, "nothing is hosted"
    # A page owns its `say`; a shared component is HANDED one, together with
    # the overlay it must build on — either way the call sites must use it.
    assert "def say(" in source or "say," in source, \
        "no safe way to notify anywhere in this module"
    assert source.count("say(") >= 4, "messages are not routed through it"


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


def test_the_sql_filter_agrees_with_the_rule_on_every_shape(con, data_dir):
    """`db.APPLIED_FIRM_SQL` is a SECOND implementation of
    `identity.holds_company` — it has to run where the paging and the counts
    do. Two hand-written copies of one rule drift, and this one decides
    whether a posting is shown at all, so the two are pinned equal over a
    generated corpus rather than argued about.

    Pinned against `holds_company` rather than against `decide`: the filter
    answers "is this company still held", while the gate also refuses a
    republication whose company is long free. One rule, two questions."""
    import datetime

    from jobdeck import db, identity

    today = datetime.date.today()

    def ago(days):
        return (today - datetime.timedelta(days=days)).isoformat()

    firms = ["Müller GmbH", "MÜLLER  GmbH", "müller gmbh ", "Müller AG",
             "a.b® GmbH", "a.b GmbH", "ab GmbH", "180° GmbH", "180 GmbH",
             "ACME™", "ACME", "", "   ", "Ⓐ GmbH", "Ⓑ GmbH", "Beispiel\xadGmbH"]
    mails = ["", "jobs@mueller.de", "JOBS@MUELLER.DE", "  ",
             "bewerbung^x@a.de", "hr@andere.de"]
    # dates on both sides of the window, plus the two that cannot be read
    ledger = [
        ("Müller GmbH", "jobs@mueller.de", ago(3)),      # deep inside
        ("Müller AG", "", ago(59)),                      # the last held day
        ("a.b® GmbH", "", ago(60)),                      # the day it reopens
        ("ACME™", "", ago(400)),                         # long free
        ("180° GmbH", "", ""),                           # no date at all
        ("Ⓐ GmbH", "", "irgendwann"),                    # unreadable
        ("", "hr@andere.de", ago(1)),                    # no company
        ("   ", "  ", ago(1)),                           # blank company
    ]
    for firma, email, sent in ledger:
        db.add_bewerbung(con, {"gesendet_am": sent, "firma": firma,
                               "email": email, "kanal": "E-Mail",
                               "status": "Gesendet"})
    for index, (firma, email) in enumerate(
            [(f, m) for f in firms for m in mails]):
        db.insert_job_if_new(con, {
            "source": "stub", "external_id": f"x{index}", "title": "Dev",
            "company": firma, "contact_email": email,
            "url": f"https://e.example/{index}"})
    con.commit()

    from jobdeck import attempts
    from jobdeck.dedupe import norm

    applications = attempts.applications(con)
    by_sql = {r[0] for r in con.execute(
        f"SELECT id FROM jobs WHERE {db.APPLIED_FIRM_SQL}",
        db.applied_firm_params(con))}
    by_rule = set()
    for job in con.execute("SELECT * FROM jobs"):
        key = norm(job["company"])
        if not key:
            continue
        at_company = [a for a in applications if norm(a.company) == key]
        if identity.holds_company(
                at_company, window_days=identity.DEFAULT_COOLDOWN_DAYS,
                today=today) is not None:
            by_rule.add(job["id"])

    assert by_sql == by_rule, (
        f"SQL-only: {sorted(by_sql - by_rule)}, rule-only: {sorted(by_rule - by_sql)}")
    assert by_rule, "the corpus produced no matches — the check would be vacuous"
    assert by_rule != {j[0] for j in con.execute("SELECT id FROM jobs")}, \
        "everything matched — the check would pass on a filter that never hides"


def test_the_sql_filter_hides_nothing_once_the_window_is_off(con, data_dir):
    from jobdeck import db
    db.add_bewerbung(con, {"gesendet_am": "2026-08-24", "firma": "Müller GmbH",
                           "email": "", "kanal": "E-Mail", "status": "Gesendet"})
    db.insert_job_if_new(con, {
        "source": "stub", "external_id": "x", "title": "Dev",
        "company": "Müller GmbH", "url": "https://e.example/1"})
    db.set_setting(con, "company_cooldown_days", "0")
    con.commit()

    hidden = {r[0] for r in con.execute(
        f"SELECT id FROM jobs WHERE {db.APPLIED_FIRM_SQL}",
        db.applied_firm_params(con))}

    assert hidden == set()


def test_a_shared_contact_address_no_longer_hides_another_companys_posting(con,
                                                                data_dir):
    """ADR 0002 keeps an address as evidence and never as an identity. One ATS
    mailbox serving two employers used to hide the second one's postings
    behind the first one's application."""
    from jobdeck import db
    db.add_bewerbung(con, {"gesendet_am": "2026-08-24", "firma": "Erste GmbH",
                           "email": "jobs@ats.example", "kanal": "E-Mail",
                           "status": "Gesendet"})
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "x", "title": "Dev",
        "company": "Zweite GmbH", "contact_email": "jobs@ats.example",
        "url": "https://e.example/1"})
    con.commit()

    hidden = {r[0] for r in con.execute(
        f"SELECT id FROM jobs WHERE {db.APPLIED_FIRM_SQL}",
        db.applied_firm_params(con))}

    assert job_id not in hidden


# ---------------------------------------------------------------------------
# The age pile: past a configurable threshold a posting leaves the working
# list — counted, one click away, never deleted (his decision, 2026-08-11).
# ---------------------------------------------------------------------------
def _aged(con, ext, days, score=80, company="Firma"):
    import datetime
    when = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    return _company_job(con, ext, f"{company} {ext}", score, when)


def test_a_posting_past_the_threshold_leaves_the_working_list(con, data_dir):
    fresh = _aged(con, "f1", days=3)
    old = _aged(con, "o1", days=90)
    con.commit()

    working = jobs._load_jobs("offen", 0)
    assert [r["id"] for r in working["rows"]] == [fresh]
    assert working["counts"]["old"] == 1
    assert working["stale_age_days"] == freshness.DEFAULT_STALE_AGE_DAYS

    pile = jobs._load_jobs("alt", 0)
    assert [r["id"] for r in pile["rows"]] == [old]


def test_a_posting_without_a_date_is_never_called_old(con, data_dir):
    """Absence of information is not evidence of staleness — the same rule the
    age penalty follows. Jooble states no publication date at all."""
    undated = _company_job(con, "u1", "Firma u1", 80)
    con.commit()

    assert [r["id"] for r in jobs._load_jobs("offen", 0)["rows"]] \
        == [undated]
    assert jobs._load_jobs("alt", 0)["rows"] == []


def test_the_threshold_he_sets_is_the_one_the_query_uses(con, data_dir):
    from jobdeck import db
    _aged(con, "m1", days=20)
    db.set_setting(con, "stale_age_days", "14")
    con.commit()

    view = jobs._load_jobs("offen", 0)
    assert view["rows"] == [] and view["counts"]["old"] == 1
    assert view["stale_age_days"] == 14
    assert "älter als 14 Tage" in jobs._hidden_line(
        view["view"], view["counts"], view["stale_age_days"])


def test_an_unreadable_threshold_falls_back_instead_of_hiding_everything(
        con, data_dir):
    from jobdeck import db
    fresh = _aged(con, "k1", days=3)
    db.set_setting(con, "stale_age_days", "")   # hand-edited, or never set
    con.commit()

    assert [r["id"] for r in jobs._load_jobs("offen", 0)["rows"]] \
        == [fresh]
    assert freshness.stale_age_setting("nonsense") == \
        freshness.DEFAULT_STALE_AGE_DAYS
    assert freshness.stale_age_setting("0") == freshness.DEFAULT_STALE_AGE_DAYS


def test_an_old_posting_is_never_deleted_only_moved(con, data_dir):
    from jobdeck import db
    old = _aged(con, "d1", days=200)
    con.commit()

    jobs._load_jobs("offen", 0)

    assert db.get_job(con, old) is not None
    assert db.get_job(con, old)["status"] == "new"


# ---------------------------------------------------------------------------
# What the row says about pay and about Arbeitnehmerüberlassung
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("job, expected", [
    ({"salary_from": "37000", "salary_to": "47000",
      "salary_period": "JAHRESGEHALT"},
     "Gehalt: 37.000 – 47.000 € (Jahresgehalt)"),
    ({"salary_from": "37000", "salary_to": "", "salary_period": ""},
     "Gehalt: 37.000 €"),
    ({"salary_from": "", "salary_to": "45000", "salary_period": "JAHRESGEHALT"},
     "Gehalt: 45.000 € (Jahresgehalt)"),
    # an hourly wage arrives in the same field as a yearly salary — measured
    # live: 30.32 to 33.69 €/h. Printed as "30 – 33" it is a different offer.
    ({"salary_from": "30.32", "salary_to": "33.69",
      "salary_period": "STUNDENLOHN"},
     "Gehalt: 30,32 – 33,69 € (Stundenlohn)"),
    # the board's own code, when it is not one we can name, stays off the row
    ({"salary_from": "55000", "salary_to": "", "salary_period": "GEHALTSSPANNE"},
     "Gehalt: 55.000 €"),
    # the board states a range on a minority of postings; the rest say nothing
    ({"salary_from": "", "salary_to": "", "salary_period": ""}, ""),
])
def test_the_row_states_the_pay_range_the_board_gave(job, expected):
    assert jobs._salary_line(job) == expected


@pytest.mark.parametrize("stored", ["inf", "-inf", "nan", "1e999", "kaputt"])
def test_a_row_can_never_be_unrenderable_because_of_what_was_stored(stored):
    """The renderer reads a value from the database, and a row that raises
    takes the whole inbox down with it — `int(float("inf"))` raises
    OverflowError, `int(float("nan"))` a ValueError."""
    assert jobs._euro(stored) == ""
    assert jobs._salary_line(
        {"salary_from": stored, "salary_to": "", "salary_period": ""}) == ""


# ---------------------------------------------------------------------------
# What a page hands to a worker thread must be callable with what it hands it.
# `run.io_bound(f)` calls f() in a thread; the TypeError from a wrong arity is
# caught by NiceGUI and written to the log, so the button simply does nothing
# and says nothing. That is how "Send now" — the app's primary send path — was
# silently inert on this branch after `_send_status` gained a parameter.
# ---------------------------------------------------------------------------
def _io_bound_calls():
    """Every `run.io_bound(name, *args)` in the UI package, as (path, node)."""
    ui_dir = pathlib.Path(jobs.__file__).parent.parent
    found = []
    for path in sorted(ui_dir.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "io_bound"
                    and node.args):
                found.append((path, node))
    return found


def _accepts(func_node: ast.FunctionDef, count: int) -> bool:
    """Can this def be called with `count` positional arguments?"""
    spec = func_node.args
    positional = spec.posonlyargs + spec.args
    required = len(positional) - len(spec.defaults)
    return required <= count and (count <= len(positional) or spec.vararg)


def test_every_io_bound_call_matches_the_arity_of_what_it_calls():
    import importlib

    calls = _io_bound_calls()
    assert len(calls) >= 15, "the scan found almost nothing — it would pass vacuously"
    checked = 0
    for path, node in calls:
        target = node.args[0]
        if not isinstance(target, ast.Name):
            continue  # a lambda or an attribute — nothing to look up by name
        module = importlib.import_module(
            f"jobdeck.ui.pages.{path.stem}" if path.parent.name == "pages"
            else f"jobdeck.ui.{path.stem}")
        defs = {n.name: n for n in ast.walk(ast.parse(path.read_text()))
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
        func = defs.get(target.id)
        if func is None or not hasattr(module, target.id):
            continue  # not a module-level function of this file
        given = len(node.args) - 1
        assert _accepts(func, given), (
            f"{path.name}:{node.lineno} calls {target.id} with {given} "
            f"argument(s) through run.io_bound, but its definition needs more — "
            f"the TypeError would be swallowed into the log and the control "
            f"would silently do nothing")
        checked += 1
    assert checked >= 10, "nothing was actually checked — the lookup is broken"


def test_an_old_posting_never_leaks_back_as_a_sibling(con, data_dir):
    """A grouped row lists the OTHER postings of its company, and that query
    takes the same filters. It needs a company with two visible postings to
    reach at all — with one, the row simply stands alone."""
    import datetime
    today = datetime.date.today()
    best = _company_job(con, "s1", "Firma", 88,
                        (today - datetime.timedelta(days=2)).isoformat())
    second = _company_job(con, "s2", "Firma", 80,
                          (today - datetime.timedelta(days=3)).isoformat())
    old = _company_job(con, "s3", "Firma", 92,
                       (today - datetime.timedelta(days=90)).isoformat())
    con.commit()

    working = jobs._load_jobs("offen", 0)

    assert [r["id"] for r in working["rows"]] == [best]
    key = working["rows"][0]["company_key"]
    assert [r["id"] for r in working["siblings"][key]] == [second], \
        "the old posting came back under the company row it was hidden from"
    assert working["rows"][0]["company_count"] == 2

    # …and in the pile the old posting is the row, with no fresh sibling
    pile = jobs._load_jobs("alt", 0)
    assert [r["id"] for r in pile["rows"]] == [old]
    assert pile["siblings"] == {}


# --------------------------------------------------------------------------
# The named views, against the database
# --------------------------------------------------------------------------
def test_reading_a_posting_takes_it_out_of_neu_but_not_out_of_the_list(
        con, data_dir):
    from jobdeck import db
    job_id = _company_job(con, "n1", "Eine GmbH", 80)
    con.commit()
    assert [r["id"] for r in jobs._load_jobs("neu", 0)["rows"]] == [job_id]

    db.mark_job_opened(con, job_id)
    con.commit()

    assert jobs._load_jobs("neu", 0)["rows"] == []
    assert [r["id"] for r in jobs._load_jobs("offen", 0)["rows"]] == [job_id]


def test_an_opened_form_counts_as_work_in_progress(con, data_dir):
    """Opening the employer's form is the whole record of a form application
    until he records it — and there need not be a draft anywhere.

    This is the case that used to be lost: of eleven form applications open on
    his real data, six had no draft at all, so a view that listed only drafts
    would have shown five of the eleven and called that "in Arbeit"."""
    from jobdeck import db
    job_id = _company_job(con, "p1", "Eine GmbH", 80)
    db.mark_form_opened(con, job_id)
    con.commit()
    assert db.get_draft_by_job(con, job_id) is None

    assert [r["id"] for r in jobs._load_jobs("in_arbeit", 0)["rows"]] == [job_id]


def test_an_opened_form_is_not_handed_back_to_the_prepare_batch(con, data_dir):
    """A started application must never be re-drafted by the batch.

    Before v10 that was free: opening a form moved the posting out of `new`.
    Now it stays in the working list, so the exclusion is by name — and every
    posting the batch takes costs a Sonnet call."""
    from jobdeck import db
    job_id = _company_job(con, "p1", "Eine GmbH", 80)
    con.execute("UPDATE jobs SET published_on=date('now') WHERE id=?", (job_id,))
    con.commit()
    assert [r["id"] for r in db.jobs_to_prepare(
        con, limit=10, max_age_days=45, min_score=50)] == [job_id]

    db.mark_form_opened(con, job_id)
    con.commit()

    assert db.jobs_to_prepare(
        con, limit=10, max_age_days=45, min_score=50) == []


def test_a_draft_being_written_counts_as_work_in_progress(con, data_dir):
    from jobdeck import db
    job_id = _company_job(con, "d1", "Eine GmbH", 80)
    db.upsert_draft(con, job_id, {"status": "generating"})
    con.commit()

    assert [r["id"] for r in jobs._load_jobs("in_arbeit", 0)["rows"]] == [job_id]


def test_nothing_in_the_corpus_is_unreachable_from_the_named_views(con, data_dir):
    """Nothing is ever deleted — so every posting has to be findable in at
    least one view, whatever he did with it."""
    from jobdeck import db
    ids = {}
    for key, status in (("plain", "new"), ("applied", "applied"),
                        ("skipped", "skipped"), ("duplicate", "duplicate")):
        job_id = _company_job(con, key, f"Firma {key}", 80)
        db.set_job_status(con, job_id, status)
        ids[key] = job_id
    # a form he has opened and not yet recorded — since v10 that posting keeps
    # its place in the working list, so it must be reachable AS one
    ids["form"] = _company_job(con, "form", "Firma form", 80)
    db.mark_form_opened(con, ids["form"])
    mismatch = _company_job(con, "mis", "Firma mis", 0)
    con.commit()

    seen = set()
    for view in jobs.VIEWS:
        seen.update(r["id"] for r in jobs._load_jobs(view.key, 0)["rows"])
    assert seen == set(ids.values()) | {mismatch}


# --------------------------------------------------------------------------
# One primary action per posting
# --------------------------------------------------------------------------
def _row(**over) -> dict:
    row = {"id": 1, "status": "new", "draft_status": None,
           "draft_updated_at": None, "apply_channel": "", "company": "Eine GmbH",
           "contact_email": "", "pdf_path": "", "url": "https://firma.de/x",
           "apply_url": "", "form_opened_at": "", "draft_room": True}
    row.update(over)
    return row


def test_a_blocked_step_says_why_beside_itself():
    """Never a tooltip: he moves through this list with the keyboard, and a
    tooltip is a thing only a mouse can find."""
    for job in (_row(status="applied"), _row(status="duplicate")):
        for step in jobs.apply_steps(job):
            assert step.enabled is False
            assert step.done or step.reason, \
                f"{step.label} refuses without saying why"


def test_an_application_at_the_firm_outranks_every_channel():
    """A company inside its cooling-off window cannot receive an application
    yet — so no step of applying may be live, whatever the channel.

    Exactly one press IS offered, and it is not an application: the one that
    answers the hold. Without it the held-back view is a room with no door.

    The reason names the day the hold lifts: the point of a window is that it
    ends, and a refusal that does not say when is indistinguishable from the
    permanent one this replaced."""
    already = _held()
    for channel in ("direct_email", "ats_form", ""):
        steps = jobs.apply_steps(_row(apply_channel=channel), already)
        live = [s for s in steps if s.enabled]
        assert [s.key for s in live] == [jobs.STEP_ANYWAY]
        assert any("zurückgestellt" in s.reason for s in steps)
        assert any("11. August 2026" in s.reason for s in steps)


# --------------------------------------------------------------------------
# What a row and a reader state
# --------------------------------------------------------------------------
@pytest.mark.parametrize("job, expected", [
    ({"match_score": 72, "age_days": 34, "apply_channel": "", "source": "jooble"},
     "34 T · Kanal offen · Jooble"),
    ({"match_score": 0, "age_days": 0, "apply_channel": "direct_email",
      "source": "arbeitsagentur"},
     "heute · E-Mail · BA"),
    ({"match_score": 85, "age_days": 1, "apply_channel": "ats_form",
      "source": "arbeitnow"},
     "1 T · Formular · Arbeitnow"),
    ({"match_score": 40, "age_days": None, "apply_channel": "unknown",
      "source": ""},
     "Datum ? · kein Weg"),
    ({"match_score": 91, "age_days": 14, "apply_channel": "direct_email",
      "source": "arbeitsagentur", "salary_from": "45000", "salary_to": "55000",
      "salary_period": "JAHRESGEHALT"},
     "14 T · E-Mail · 45–55 T€ · BA"),
    # A posting the batch has not reached yet says so, FIRST — the row draws
    # an em dash where the number goes, and an em dash is this app's word for
    # "the advert states nothing", not for "nobody has looked yet".
    ({"match_score": None, "status": "new", "age_days": 0,
      "apply_channel": "", "source": "arbeitnow"},
     "noch nicht bewertet · heute · Kanal offen · Arbeitnow"),
    # And one nothing is coming for loses the "noch". The batch skips a hidden
    # company on purpose, so a promise there would never be kept.
    ({"match_score": None, "status": "new", "company_hidden": True,
      "age_days": 3, "apply_channel": "", "source": "arbeitnow"},
     "nicht bewertet · 3 T · Kanal offen · Arbeitnow"),
    ({"match_score": None, "status": "duplicate", "age_days": 3,
      "apply_channel": "", "source": "arbeitnow"},
     "nicht bewertet · 3 T · Kanal offen · Arbeitnow"),
])
def test_the_row_line_states_only_facts_the_posting_carries(job, expected):
    assert jobs.row_meta(job) == expected


def test_a_scored_posting_says_nothing_about_being_scored():
    """The line is for what is missing. Every row carrying "bewertet" would be
    four hundred rows of noise around the handful that mean something — and
    the score is already drawn at the end of the row above it."""
    scored = {"match_score": 0, "status": "new", "age_days": 2,
              "apply_channel": "", "source": "jooble"}
    assert "bewertet" not in jobs.row_meta(scored)


def test_an_hourly_wage_is_never_abbreviated_to_thousands():
    """The same column carries 55000.0 for a year and 30.32 for an hour;
    '30 T€' would be a different offer entirely."""
    assert jobs._salary_short(
        {"salary_from": "30.32", "salary_to": "35.50",
         "salary_period": "STUNDENLOHN"}) == ""
    assert jobs._salary_short(
        {"salary_from": "45000", "salary_to": "55000",
         "salary_period": "JAHRESGEHALT"}) == "45–55 T€"


def test_the_facts_block_states_an_absence_rather_than_dropping_the_row():
    """"Gehalt —" says the advert is silent; a row that simply vanished would
    leave him wondering whether the app had lost it."""
    facts = dict(jobs.reader_facts(
        {"refnr": "", "location": "", "salary_from": "", "salary_to": "",
         "salary_period": "", "apply_channel": "", "ats_vendor": "",
         "published_on": "", "fetched_at": ""}))
    assert facts["Refnr"] == "—"
    assert facts["Ort"] == "—"
    assert facts["Gehalt"] == "—"
    assert facts["Kanal"] == "noch nicht ermittelt"


def test_the_warnings_stand_above_the_advert_worst_first():
    job = {"liveness": "gone", "liveness_checked_at": "2026-08-01T09:00:00",
           "temp_agency": 1, "draft_status": None, "draft_updated_at": None}
    already = _held()
    notes = jobs.reader_notes(job, already)
    assert [kind for _, kind in notes] == ["danger", "danger", "warn"]
    assert "zurückgestellt" in notes[0][0]
    assert "offline" in notes[1][0]
    assert "Arbeitnehmerüberlassung" in notes[2][0]


def test_a_quiet_posting_carries_no_warnings_at_all():
    assert jobs.reader_notes(
        {"liveness": "", "liveness_checked_at": "", "temp_agency": 0,
         "draft_status": None, "draft_updated_at": None}, None) == []


def test_a_posting_he_put_away_offers_nothing_until_it_comes_back():
    """The "Kein Interesse" view had no exit at all: nothing wrote a status
    back to 'new', so pressing x on the wrong row was permanent — while the
    channel arms below still offered to write an application for it. The way
    back is the triage row's own button; no apply step may be live meanwhile."""
    steps = jobs.apply_steps(_row(status="skipped", apply_channel="direct_email"))
    assert not any(s.enabled for s in steps)
    assert any("weggelegt" in s.reason for s in steps)


def test_a_form_posting_can_still_be_given_a_letter():
    """The current complete-package flow writes a letter for form postings.

    ADR 0005 defines the target as job-specific, versioned document selection.
    """
    for channel in ("ats_form", "board_apply", "company_site", "unknown"):
        steps = {s.key: s for s in jobs.apply_steps(_row(apply_channel=channel))}
        assert steps[jobs.STEP_START].enabled, channel
        assert "0,09 $" in steps[jobs.STEP_START].label, channel
    # …and never where an application cannot happen. Under a liftable hold the
    # only live press answers the hold; STEP_START itself stays refused.
    assert not any(s.enabled for s in jobs.apply_steps(_row(status="applied")))
    held = {s.key: s for s in jobs.apply_steps(
        _row(apply_channel="ats_form"), _held())}
    assert held[jobs.STEP_START].enabled is False
    assert held[jobs.STEP_ANYWAY].enabled is True


def test_a_resolved_dead_end_is_described_the_same_way_everywhere():
    """One screen said "kein Weg", "noch nicht ermittelt" and "kein
    Bewerbungsweg gefunden" about one posting, and the middle one was false —
    the channel HAD been resolved."""
    job = _row(apply_channel="unknown", ats_vendor="")
    assert jobs._apply_line(job) == "Kein Bewerbungsweg gefunden"
    assert dict(jobs.reader_facts({**job, "refnr": "", "location": "",
                                   "salary_from": "", "salary_to": "",
                                   "salary_period": "", "published_on": "",
                                   "fetched_at": ""}))["Kanal"] \
        == "Kein Bewerbungsweg gefunden"


def test_the_verdict_is_headed_with_the_score_it_reasons_about():
    """The model was asked about the posting, not about its age. Heading its
    paragraph with the aged number produced "WARUM 72" over an argument for a
    near-perfect match that never mentions how old the advert is."""
    assert jobs._verdict_heading({"match_score": 92, "effective_score": 92}) \
        == "WARUM 92"
    assert jobs._verdict_heading({"match_score": 92, "effective_score": 72}) \
        == "WARUM 92 · durch das Alter noch 72"


def test_the_line_under_the_list_owns_up_to_the_search_and_the_read_pile():
    """It claimed to be "derived from the filters it actually used" while
    ignoring the two that define the landing view: the search box and the
    postings he has already read."""
    line = jobs._hidden_line(jobs.view_for("neu"),
                             {"read": 186, "mismatches": 335, "dead": 74,
                              "applied_firm": 0, "old": 129},
                             45, search=" django ")
    assert line.startswith("gefiltert nach „django“")
    assert "186 schon gelesen" in line
    # and "Alle offen" hides the piles but not the read ones
    assert "schon gelesen" not in jobs._hidden_line(
        jobs.view_for("offen"), {"read": 186, "mismatches": 0, "dead": 0,
                                 "applied_firm": 0, "old": 0}, 45)


def test_the_advert_is_not_cut_where_his_corpus_lives():
    """His longest stored posting is about 28k characters; the cut existed at
    8k with no ellipsis, no note and no way to see the rest."""
    assert jobs.DESCRIPTION_LIMIT >= 40_000


# --------------------------------------------------------------------------
# Applying, as the acts it really takes
# --------------------------------------------------------------------------
def _steps(job, already=None):
    return {s.key: s for s in jobs.apply_steps(job, already)}


def test_an_email_posting_offers_writing_and_sending_and_nothing_else():
    """By e-mail the editor builds the Mappe on the way, so there is no
    separate step for it and no form to open."""
    steps = _steps(_row(apply_channel="direct_email", contact_email="hr@x.de"))
    assert list(steps) == [jobs.STEP_DRAFT, jobs.STEP_SEND, jobs.STEP_RECORD]
    assert steps[jobs.STEP_DRAFT].label == "E-Mail-Bewerbung schreiben"
    assert steps[jobs.STEP_SEND].enabled is False, "nothing to send yet"
    assert "Anschreiben" in steps[jobs.STEP_SEND].reason


def test_a_form_posting_is_two_presses():
    """It was four — Anschreiben, Mappe, Formular, eintragen — and he pressed
    the third and skipped the rest. One press opens their page and prepares
    everything behind it; one says it went out."""
    steps = _steps(_row(apply_channel="ats_form", url="https://firma.de/stelle"))
    assert list(steps) == [jobs.STEP_START, jobs.STEP_RECORD]
    assert steps[jobs.STEP_START].enabled is True
    assert steps[jobs.STEP_RECORD].label == "Abgeschickt"
    # the price is ON the button: that is the disclosure, not a dialog
    assert steps[jobs.STEP_START].label == "Bewerbung starten · ~0,09 $"


def test_a_started_form_stops_offering_to_start_again():
    """A second press would re-claim the draft and clear the Mappe pointer
    while the staged file still holds the old letter — the two would disagree
    with no error anywhere."""
    steps = _steps(_row(apply_channel="ats_form", url="https://firma.de/x",
                        form_opened_at="2026-08-15T20:00:00"))
    assert steps[jobs.STEP_START].done is True
    assert steps[jobs.STEP_START].enabled is False
    assert jobs._next_step(list(steps.values())) == 1   # "Abgeschickt" is next


def test_the_daily_letter_limit_refuses_the_press_and_says_where_to_raise_it():
    """His decision (2026-08-15): a HARD cap. Every form application now writes
    a letter, so the form path spends money per press — and the screen must
    refuse exactly what `drafting._claim` refuses, or it promises a letter the
    gate below it will not write."""
    steps = _steps(_row(apply_channel="ats_form", url="https://firma.de/x",
                        draft_room=False))
    assert steps[jobs.STEP_START].enabled is False
    assert "Tageslimit" in steps[jobs.STEP_START].reason
    assert "Einstellungen" in steps[jobs.STEP_START].reason
    # …and with room it is live again
    assert _steps(_row(apply_channel="ats_form", url="https://firma.de/x",
                       draft_room=True))[jobs.STEP_START].enabled is True


def test_a_posting_with_no_openable_address_cannot_be_started():
    steps = _steps(_row(apply_channel="ats_form", url="", apply_url=""))
    assert steps[jobs.STEP_START].enabled is False
    assert "Adresse" in steps[jobs.STEP_START].reason


def test_an_unresolved_posting_is_never_asked_about():
    """"Kanal ermitteln" is not a step any more. The scheduler resolves the
    whole backlog every half hour, and the press resolves inline when it has
    not got there yet — a button whose entire content is "ask the question you
    already asked me to answer" rendered on four rows out of five."""
    assert jobs.STEP_RESOLVE not in _steps(_row())
    assert list(_steps(_row())) == [jobs.STEP_START, jobs.STEP_RECORD]


def test_recording_is_offered_once_something_was_actually_started():
    """A form application can only be recorded after one was begun, and that
    is a guard rather than tidiness.

    On the form path the step list is two entries long, so whenever
    "Bewerbung starten" is refused — the daily letter cap used up, or a
    posting with no openable address — an ungated "Abgeschickt" became the
    first ENABLED step: drawn as the SOLID recommended button, and run by ⏎
    under a cursor moving down the list. That writes a ledger row for a form
    that was never opened, permanently spending the company's only slot.

    The e-mail path keeps it open: he may have written and sent one by hand."""
    for job in (_row(), _row(apply_channel="ats_form"),
                _row(apply_channel="ats_form", draft_room=False),
                _row(apply_channel="ats_form", url="", apply_url="")):
        step = _steps(job)[jobs.STEP_RECORD]
        assert step.enabled is False
        assert "Erst die Bewerbung starten" in step.reason

    for job in (_row(apply_channel="direct_email", contact_email="hr@x.de"),
                _row(apply_channel="ats_form",
                     form_opened_at="2026-08-15T20:00:00")):
        assert _steps(job)[jobs.STEP_RECORD].enabled is True

    # and nothing is ever the solid button when nothing may be pressed
    refused = jobs.apply_steps(_row(apply_channel="ats_form", draft_room=False))
    assert jobs._next_step(refused) == -1


def test_a_written_letter_marks_its_step_done_and_opens_the_next():
    steps = _steps(_row(apply_channel="direct_email", contact_email="hr@x.de",
                        draft_status="ready"))
    assert steps[jobs.STEP_DRAFT].done is True
    assert steps[jobs.STEP_DRAFT].label == "Anschreiben neu schreiben"
    assert steps[jobs.STEP_SEND].enabled is True


def test_the_press_is_marked_done_by_the_stamp_not_by_the_documents():
    """A built Mappe is not evidence that he opened anyone's form — the
    specimen Mappe on the Unterlagen screen is built from the same code path.
    `form_opened_at` is the only thing that means an application started."""
    with_pdf = _steps(_row(apply_channel="ats_form", draft_status="ready",
                           pdf_path="/tmp/mappe.pdf"))
    assert with_pdf[jobs.STEP_START].done is False
    started = _steps(_row(apply_channel="ats_form",
                          form_opened_at="2026-08-15T20:00:00"))
    assert started[jobs.STEP_START].done is True


@pytest.mark.parametrize("job, already", [
    (_row(status="applied", apply_channel="ats_form"), None),
    (_row(status="duplicate", apply_channel="ats_form"), None),
    (_row(status="skipped", apply_channel="ats_form"), None),
    (_row(apply_channel="ats_form", url="https://firma.de/x"),
     _held()),
])
def test_where_no_application_can_happen_no_step_is_live(job, already):
    """Including "Formular öffnen": opening it MOVES the posting to `portal`,
    which is this app's record that an application has begun. Reading the
    advert is still a press away in the triage row, and that changes nothing.

    A liftable hold is the one exception and has its own test: there the press
    on offer answers the hold rather than starting an application."""
    steps = jobs.apply_steps(job, already)
    assert steps, "the posting offers nothing at all"
    assert not any(s.enabled for s in steps if s.key != jobs.STEP_ANYWAY)
    assert all(s.reason for s in steps
               if not s.done and s.key != jobs.STEP_ANYWAY)


def test_the_step_to_press_is_the_first_that_is_neither_done_nor_refused():
    steps = jobs.apply_steps(_row(apply_channel="ats_form",
                                  draft_status="ready",
                                  url="https://firma.de/x"))
    assert steps[jobs._next_step(steps)].key == jobs.STEP_START
    assert jobs._next_step([jobs.Step("a", "A", done=True),
                            jobs.Step("b", "B", enabled=False)]) == -1


def test_a_posting_whose_form_he_opened_can_come_back():
    """Opening a form he then decided against used to take the posting out of
    the working list for good. It is not a REFUSAL either: he may still finish
    that application, so recording it stays live."""
    steps = jobs.apply_steps(_row(form_opened_at="2026-08-15T20:00:00",
                                  apply_channel="ats_form",
                                  url="https://firma.de/x"))
    assert steps[-1].key == jobs.STEP_RECORD
    assert steps[-1].enabled is True
    assert jobs._blocking_reason(
        _row(form_opened_at="2026-08-15T20:00:00"), None) == ""


def test_a_letter_already_on_its_way_says_where_to_resolve_it():
    """A draft that is `sending` or `sent` is the record of what went out, and
    only the Postausgang can tell those two apart. It must be named by the
    name it wears: this line said "Review queue" for two slices after the
    screen stopped being called that."""
    from jobdeck.ui.layout import BEWERBUNGEN_TABS
    steps = {s.key: s for s in jobs.apply_steps(
        _row(apply_channel="direct_email", contact_email="hr@x.de",
             draft_status="sending"))}
    assert steps[jobs.STEP_SEND].enabled is False
    label = next(label for key, label, _ in BEWERBUNGEN_TABS
                 if key == "postausgang")
    assert label in steps[jobs.STEP_SEND].reason


def _own_nodes(func):
    """Every node belonging to THIS function, excluding nested definitions.

    Without the exclusion an `await` inside a nested handler would be read as
    an await of the outer function, and a perfectly ordered outer handler
    would be reported as an offender."""
    stack, own = list(func.body), []
    while stack:
        node = stack.pop()
        own.append(node)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef | ast.FunctionDef
                          | ast.Lambda):
                continue
            stack.append(child)
    return own


@pytest.mark.parametrize("page_module", _overlay_files())
def test_no_handler_clears_the_overlay_after_an_await(page_module):
    """`overlay.clear()` before an await is housekeeping; after one it is a
    demolition. The page stays fully interactive while a handler waits on a
    model call or a Chrome render, so a dialog opened in that window is
    destroyed — and in NiceGUI clearing the slot drops the dialog's canary
    element, whose finalizer calls `Dialog.delete()` without ever setting
    `value`. A coroutine parked on `await confirm` is then never resumed at
    all: the confirmation vanishes and its handler hangs for the life of the
    page.

    Found by review on `propose_claims`, which awaited a multi-second model
    call and then cleared. Pinned as a class, not as one line.
    """
    path = pathlib.Path(jobs.__file__).parent / page_module
    tree = ast.parse(path.read_text())

    offenders, scanned = [], 0
    for func in ast.walk(tree):
        if not isinstance(func, ast.AsyncFunctionDef):
            continue
        own = _own_nodes(func)
        first_await = min((n.lineno for n in own if isinstance(n, ast.Await)),
                          default=None)
        clears = [n.lineno for n in own if isinstance(n, ast.Call)
                  and ast.unparse(n.func) == "overlay.clear"]
        if clears:
            scanned += 1
        if first_await is None:
            continue
        offenders += [f"{path.name}:{func.name}:{line}"
                      for line in clears if line > first_await]
    assert offenders == [], (
        "overlay.clear() runs after an await at " + ", ".join(offenders)
        + " — a dialog opened while that await was pending is destroyed, and "
          "anything parked on it never resolves")
    if page_module in ("jobs.py", "unterlagen.py", "bewerbungen.py"):
        assert scanned, "no handler clears the overlay at all — scan is broken"


# --------------------------------------------------------------------------
# The "Läuft" strip: what is under way, above the list
# --------------------------------------------------------------------------
def test_the_strip_survives_a_search_and_a_view_that_hides_everything(
        con, data_dir):
    """An application under way is not a search result. Losing the posting he
    had started is the complaint this whole slice answers, so it has to be on
    screen whichever view he is in, on whatever page, however he searched."""
    from jobdeck import db
    job_id = _company_job(con, "p1", "Eine GmbH", 80)
    db.mark_form_opened(con, job_id)
    con.commit()

    hidden = jobs._load_jobs("in_arbeit", 0, search="etwas-das-nicht-existiert")
    assert hidden["rows"] == []
    assert [r["id"] for r in hidden["started"]] == [job_id]

    elsewhere = jobs._load_jobs("beworben", 0)
    assert [r["id"] for r in elsewhere["started"]] == [job_id]


@pytest.mark.parametrize("minutes, expected", [
    (0, "Formular bei Eine GmbH — gerade eben"),
    (3, "Formular bei Eine GmbH — seit 3 Min."),
    (9, "Formular bei Eine GmbH — seit 9 Min."),
    # past ten it stops reporting and starts asking — a LABEL, not a prompt,
    # so there is nothing to dismiss and nothing to learn to dismiss
    (10, "Formular bei Eine GmbH — abgeschickt?"),
    (600, "Formular bei Eine GmbH — abgeschickt?"),
    # one minute inside the receipt window the short question still stands …
    (72 * 60 - 1, "Formular bei Eine GmbH — abgeschickt?"),
    # … and past it the strip stops implying a receipt is coming. The 72 is
    # the SAME constant the reply reader matches receipts within — one
    # number, or a receipt at 60 hours falls between two claims.
    (72 * 60, "Formular bei Eine GmbH — abgeschickt? Keine "
              "Eingangsbestätigung nach 3 Tagen; viele Portale "
              "schicken keine."),
])
def test_a_running_application_reports_then_asks(minutes, expected):
    import datetime
    now = datetime.datetime(2026, 8, 15, 20, 0, 0)
    started = now - datetime.timedelta(minutes=minutes)
    assert jobs.started_line(
        {"company": "Eine GmbH", "form_opened_at": started.isoformat()},
        now=now) == expected


def test_a_form_opened_before_the_app_could_stamp_it_says_so():
    """Three of his eleven left no evidence of when. A computed age would sort
    them among applications begun this minute, which is the one thing the
    migration refused to do — the screen must not undo it."""
    from jobdeck import constants
    assert jobs.started_line({"company": "Eine GmbH",
                              "form_opened_at": constants.FORM_OPENED_UNKNOWN}) \
        == "Formular bei Eine GmbH — seit unbekannt"


@pytest.mark.parametrize("job, expected, kind", [
    ({"mappe_kind": "vollständig", "draft_status": "ready"},
     "Mappe bereit", ""),
    ({"mappe_kind": "", "draft_status": "generating"},
     "Mappe wird gebaut …", ""),
    # The legacy flow exposes complete-package or missing-package states, so a
    # missing package must be explicit rather than offered for upload.
    ({"mappe_kind": "", "draft_status": "failed"},
     "Mappe NICHT fertig — von Hand hochladen", "warn"),
    ({"mappe_kind": "", "draft_status": None},
     "Mappe NICHT fertig — von Hand hochladen", "warn"),
])
def test_the_strip_says_when_the_documents_are_not_complete(job, expected, kind):
    assert jobs.mappe_line(job) == (expected, kind)


def test_the_employers_tab_opens_before_anything_is_awaited():
    """The ONE mechanical statement of the press's order.

    A `window.open` pushed after a minute of server work is what a popup
    blocker refuses, and the letter takes about a minute. Server-side there is
    no synchronous gesture path in this framework, so "before the first await"
    is both the closest achievable rule and the only one a test can hold.

    Written as an AST rule because the failure is silent: the tab simply never
    appears, the letter is written anyway, and nothing raises."""
    source = pathlib.Path(jobs.__file__).read_text()
    func = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "start_application")
    navigations = [n.lineno for n in ast.walk(func)
                   if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute) and n.func.attr == "to"]
    awaits = [n.lineno for n in ast.walk(func) if isinstance(n, ast.Await)]
    assert navigations, "the press never opens the employer's page"
    assert awaits, "the press does no work at all"
    assert min(navigations) < min(awaits), (
        "the employer's tab is opened after an await — a popup blocker "
        "refuses a window.open pushed that late")


def test_the_press_never_records_an_application_by_itself():
    """The app cannot see whether he pressed the employer's submit, and a
    false ledger row permanently spends that company's only slot, silently.

    So the one press writes the moment, the letter and the documents — and
    never the application. Only `confirm_applied` may do that."""
    source = pathlib.Path(jobs.__file__).read_text()
    func = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "start_application")
    # Every NAME the function mentions, not only the ones in callee position.
    # This codebase's idiom for exactly this work is
    # `await run.io_bound(_record_application, job_id, kanal)`, where the
    # recorder is an ARGUMENT — a rule that collected callees alone could not
    # see the one shape it exists to forbid.
    mentioned = {n.attr for n in ast.walk(func) if isinstance(n, ast.Attribute)}
    mentioned |= {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
    for forbidden in ("_record_application", "record_application",
                      "record_form_application", "apply_job",
                      "confirm_applied", "_undo_bar", "add_bewerbung"):
        assert forbidden not in mentioned, (
            f"start_application names {forbidden} — nothing may write an "
            f"application on a timer or a guess")


def test_the_page_records_the_very_signature_the_watcher_compares(con, data_dir):
    """The list-screen equivalent of "signature FIRST": the loader used to hand
    the watcher a bare `data_signature` while `_signature()` compared that PLUS
    every watched setting. A 38-tuple recorded against a 46-tuple is never
    equal, so every tick counted as a change and the screen rebuilt itself on a
    timer for the life of the page — defeating the one property ui/live.py
    exists to provide, and it had been so since the first setting was watched.
    """
    _company_job(con, "a", "Firma", 80)
    con.commit()

    recorded = jobs._load_jobs("neu", 0)["signature"]

    assert recorded == jobs._signature()
    # …and it really does carry the settings, so a change to one is seen
    from jobdeck import db
    db.set_setting(con, jobs.MIN_SCORE_SETTING, "60")
    con.commit()
    assert jobs._signature() != recorded


def test_a_posting_that_repeats_an_applied_position_leaves_the_working_list(
        con, data_dir):
    """It can never become a second application, so it goes where every other
    posting that cannot goes — counted beneath the list, one click away, never
    deleted. Before this it stayed in the list with every button dead, taking
    a slot it could not use."""
    from jobdeck import db
    from jobdeck.ui.pages import jobs as jobs_page

    bew = db.add_bewerbung(con, {"gesendet_am": "2026-01-05", "firma": "Beispiel GmbH",
                                 "kanal": "E-Mail", "status": "Absage"})
    con.execute(
        "INSERT INTO application_attempts (idempotency_key, state, company,"
        " company_key, position, channel, bewerbung_id, created_at, updated_at)"
        " VALUES ('bewerbung:x', 'recorded', 'Beispiel GmbH', 'beispiel gmbh',"
        " 'Software Developer: Python', 'E-Mail', ?, '2026-01-05', '2026-01-05')",
        (bew,))
    repeat = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "repost", "title": "Software Developer: Python",
        "company": "Beispiel GmbH", "url": "https://e.example/1"})
    other = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "other", "title": "Ganz andere Stelle",
        "company": "Beispiel GmbH", "url": "https://e.example/2"})
    con.commit()

    # UNGROUPED on purpose: `list_job_groups` returns one row per company, so
    # a grouped assertion would pass because the sibling represented the
    # company — not because the filter hid anything.
    working = next(v for v in jobs_page.VIEWS if v.key == "offen")
    rows = db.list_jobs(con, status=working.status, limit=100,
                        **working.filters)
    assert repeat not in {r["id"] for r in rows}
    # …and the company's window is long past, so it is THIS rule hiding it
    assert other in {r["id"] for r in rows}
    assert db.count_republication_jobs(con, "new") == 1

    pile = next(v for v in jobs_page.VIEWS if v.key == "gleiche_stelle")
    shown = db.list_jobs(con, status=pile.status, limit=100, **pile.filters)
    assert {r["id"] for r in shown} == {repeat}, "the door has to open onto it"


def test_the_republication_filter_agrees_with_the_rule_on_every_shape(
        con, data_dir):
    """`db.REPUBLICATION_SQL` is a SECOND implementation of
    `identity.republication_of` — it runs where the paging and the counts do.
    Pinned equal over a generated corpus rather than argued about."""
    from jobdeck import attempts, db, identity
    from jobdeck.dedupe import norm

    firms = ["Müller GmbH", "MÜLLER  GmbH", "Müller AG", "a.b® GmbH", "a.b GmbH",
             "", "   "]
    titles = ["Python Dev", "python  dev ", "PYTHON DEV", "Senior Python Dev",
              "", "   ", "Entwickler (m/w/d)"]
    ledger = [("Müller GmbH", "Python Dev"), ("a.b® GmbH", "Entwickler (m/w/d)"),
              ("Müller AG", ""), ("", "Python Dev")]
    for firma, position in ledger:
        bew = db.add_bewerbung(con, {"gesendet_am": "2026-01-05", "firma": firma,
                                     "kanal": "E-Mail", "status": "Absage"})
        con.execute(
            "INSERT INTO application_attempts (idempotency_key, state, company,"
            " company_key, position, channel, bewerbung_id, created_at, updated_at)"
            " VALUES (?, 'recorded', ?, ?, ?, 'E-Mail', ?, '2026-01-05','2026-01-05')",
            (f"bewerbung:{bew}", firma, norm(firma), position, bew))
    for index, (firma, title) in enumerate([(f, t) for f in firms for t in titles]):
        db.insert_job_if_new(con, {
            "source": "stub", "external_id": f"r{index}", "title": title,
            "company": firma, "url": f"https://e.example/{index}"})
    con.commit()

    applications = attempts.applications(con)
    by_sql = {r[0] for r in con.execute(
        f"SELECT id FROM jobs WHERE {db.REPUBLICATION_SQL}")}
    by_rule = set()
    for job in con.execute("SELECT * FROM jobs"):
        key = norm(job["company"])
        if not key:
            continue
        at_company = [a for a in applications if norm(a.company) == key]
        if identity.republication_of(at_company, job["title"]) is not None:
            by_rule.add(job["id"])

    assert by_sql == by_rule, (
        f"SQL-only: {sorted(by_sql - by_rule)}, rule-only: {sorted(by_rule - by_sql)}")
    assert by_rule, "the corpus produced no matches — the check would be vacuous"
    assert by_rule != {j[0] for j in con.execute("SELECT id FROM jobs")}, \
        "everything matched — the check would pass on a filter that never hides"
