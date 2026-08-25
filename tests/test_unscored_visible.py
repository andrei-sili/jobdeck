"""A posting nobody has graded yet, and how the screen says so.

Discovery hands postings over faster than the batch grades them — twenty every
ten minutes — so for those minutes the list holds rows with no score. It drew
an em dash where the number goes, which is this app's word for "the advert
states nothing", and he read a screenful of them and asked why the app had
stopped working. It had not; it was working and saying nothing about it.

The property under test is not the wording but the PROMISE: "noch nicht
bewertet" says a worker is coming, and it may only be written where one is.
"""

import pytest

from jobdeck import db
from jobdeck.ui.pages import jobs


def _job(con, company="Eine GmbH", *, score=70, status="new", external="e1",
         title="Entwickler"):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": external, "title": title,
        "company": company, "url": "https://example.invalid/1",
    })
    if score is not None:
        db.set_job_score(con, job_id, score, "weil")
    if status != "new":
        db.set_job_status(con, job_id, status)
    con.commit()
    return job_id


# ---------------------------------------------------------------------------
# The differential: the screen's predicate and the batch's SELECT
# ---------------------------------------------------------------------------
def test_the_row_promises_a_score_exactly_where_the_batch_will_write_one(con):
    """The one test this slice turns on.

    `awaits_score` is a second copy of the rule `db.list_unscored_jobs`
    already expresses in SQL, and a second copy of a rule is how a screen
    starts saying something the worker underneath it does not do. Driven
    through `_load_jobs` rather than against hand-built dicts, because the
    third arm of the rule — the hidden company — reaches a row only through
    the loader, which marks it by construction.
    """
    wanted = _job(con, "Wartende GmbH", score=None, external="a")
    _job(con, "Bewertete GmbH", score=82, external="b")
    hidden_unscored = _job(con, "Versteckte GmbH", score=None, external="c")
    hidden_scored = _job(con, "Versteckte GmbH", score=55, external="d",
                         title="Zweite Stelle")
    duplicate = _job(con, "Doppelte GmbH", score=None, status="duplicate",
                     external="e")
    applied = _job(con, "Beworbene GmbH", score=None, status="applied",
                   external="f")
    skipped = _job(con, "Weggelegte GmbH", score=None, status="skipped",
                   external="g")
    db.hide_company(con, "Versteckte GmbH")
    con.commit()

    queue = {row["id"] for row in db.list_unscored_jobs(con, limit=999)}
    assert queue == {wanted}, "the batch's own answer, before anything is drawn"

    seen: set[int] = set()
    for view in jobs.VIEWS:
        loaded = jobs._load_jobs(view.key, 0, min_score=0)
        rows = loaded["rows"] + [row for group in loaded["siblings"].values()
                                 for row in group]
        for row in rows:
            seen.add(row["id"])
            assert jobs.awaits_score(row) == (row["id"] in queue), (
                f"view {view.key!r} disagrees with the batch on "
                f"{row['company']} ({row['id']})")

    # A predicate agreeing on nothing agrees with everything: the views have to
    # have actually carried every shape past the assertion above.
    assert seen >= {wanted, hidden_unscored, hidden_scored, duplicate,
                    applied, skipped}


def test_only_a_queued_posting_is_promised_a_score(con):
    """Stated as the four shapes, so the differential above cannot pass by
    finding nothing. A hidden company is skipped on purpose — a score is a
    paid call and a nineteen-branch agency he put out of sight would take the
    budget for ever — so the word "noch" would be a promise nothing keeps."""
    assert jobs.awaits_score({"match_score": None, "status": "new"}) is True
    assert jobs.awaits_score({"match_score": 0, "status": "new"}) is False
    assert jobs.awaits_score({"match_score": None, "status": "duplicate"}) is False
    assert jobs.awaits_score({"match_score": None, "status": "new",
                              "company_hidden": True}) is False


# ---------------------------------------------------------------------------
# What the line over the list says
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("pending, on, expected", [
    (0, True, ""),
    (0, False, ""),
    (1, True, "1 Anzeige wartet noch auf die Bewertung."),
    (18, True, "18 Anzeigen warten noch auf die Bewertung."),
    (1, False, "1 Anzeige ohne Bewertung — die Bewertung ist ausgeschaltet."),
    (18, False, "18 Anzeigen ohne Bewertung — die Bewertung ist ausgeschaltet."),
])
def test_the_line_over_the_list_counts_and_declines_to_promise(
        pending, on, expected):
    """Every figure here can be one, and this project has shipped "1 Anzeigen"
    before. And the sentence changes shape entirely when nothing is coming:
    "warten" is a promise, and with scoring switched off — this app's own
    default — that queue would sit there for ever."""
    assert jobs.scoring_line(pending, on) == expected


def test_nothing_waiting_says_nothing_at_all():
    """A permanent "0 warten auf Bewertung" is a line you stop reading, and
    this one has to be noticed on the ten minutes it is about."""
    assert jobs.scoring_line(0, True) == ""
    assert jobs.scoring_line(-3, True) == ""


# ---------------------------------------------------------------------------
# What the reading pane says
# ---------------------------------------------------------------------------
def test_the_reading_pane_promises_only_while_a_worker_can_run():
    queued = {"match_score": None, "status": "new"}
    assert "läuft im Hintergrund" in jobs.unscored_note(queued, True)
    note = jobs.unscored_note(queued, False)
    assert "läuft im Hintergrund" not in note
    assert "ausgeschaltet" in note


def test_the_reading_pane_names_the_reason_a_posting_is_skipped():
    """"Wird nicht bewertet" over a posting whose company he hid himself is a
    fact he can act on; the same sentence with no reason is a shrug."""
    hidden = {"match_score": None, "status": "new", "company_hidden": True}
    assert "ausgeblendet" in jobs.unscored_note(hidden, True)
    assert "noch" not in jobs.unscored_note(hidden, True)


def test_the_block_over_an_ungraded_posting_is_not_headed_with_a_question():
    """The heading names what the block holds. "WARUM" over "this has not been
    graded yet" asks something the paragraph beneath it does not answer."""
    assert jobs._verdict_heading({"match_score": None}) == "BEWERTUNG"
    assert jobs._verdict_heading(
        {"match_score": 92, "effective_score": 92}) == "WARUM 92"


# ---------------------------------------------------------------------------
# The watcher has to be able to see what the line states
# ---------------------------------------------------------------------------
def test_a_score_landing_moves_the_page_signature(con):
    """Without this the line is stale for exactly the ten minutes it exists to
    describe: the backlog drains and the screen goes on printing the number it
    printed when he opened it."""
    before = jobs.signature_of(con)
    job_id = _job(con, "Wartende GmbH", score=None, external="a")
    arrived = jobs.signature_of(con)
    assert arrived != before, "a posting arriving has to move it"

    db.set_job_score(con, job_id, 74, "weil")
    con.commit()
    assert jobs.signature_of(con) != arrived, "a score landing has to move it"


def test_hiding_a_company_moves_the_page_signature(con):
    """The third arm of the same rule: hiding an employer takes its postings
    out of the queue without touching a single row of `jobs`."""
    _job(con, "Versteckte GmbH", score=None, external="a")
    before = jobs.signature_of(con)
    db.hide_company(con, "Versteckte GmbH")
    con.commit()
    assert jobs.signature_of(con) != before


def test_the_scoring_switch_moves_the_page_signature(con):
    """No table signature moves when a setting flips, so the switch this line
    reads had to join the watched list. Without it the sentence would go on
    promising a worker he had just switched off."""
    before = jobs.signature_of(con)
    db.set_setting(con, "ai_enabled", "1")
    con.commit()
    assert jobs.signature_of(con) != before


def test_the_reader_is_redrawn_when_the_scoring_switch_flips():
    """The page signature decides whether to re-read; the row fingerprint
    decides whether to re-draw. The pane STATES this fact, so it has to be in
    the second one too — it is not a column, so nothing else would carry it."""
    row = {"id": 1, "match_score": None, "status": "new", "scoring_on": True}
    assert jobs._row_fingerprint(row) != jobs._row_fingerprint(
        {**row, "scoring_on": False})


def test_the_loader_hands_the_screen_both_facts(con, monkeypatch):
    """The count is asked the way the BATCH selects, not the way the current
    view filters: it is a fact about the worker. A figure that followed the
    view would read 0 in "Beworben" and tell him everything was graded on the
    one screen where nothing is."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _job(con, "Wartende GmbH", score=None, external="a")
    _job(con, "Bewertete GmbH", score=82, external="b")
    con.commit()

    for view in ("neu", "offen", "beworben", "doppelt"):
        loaded = jobs._load_jobs(view, 0, min_score=0)
        assert loaded["pending_scores"] == 1, view
        assert loaded["scoring_on"] is False, view


def test_every_row_carries_the_switch_the_pane_reads(con):
    """Read once and carried, like the drafting cap beside it: two reads of
    one fact are two chances to say different things about it."""
    _job(con, "Wartende GmbH", score=None, external="a")
    con.commit()
    loaded = jobs._load_jobs("offen", 0, min_score=0)
    assert loaded["rows"], "the corpus has to reach the assertion"
    for row in loaded["rows"]:
        assert row["scoring_on"] == loaded["scoring_on"]


def test_the_rail_and_the_list_count_the_same_backlog(con):
    """Two screens, one number. The rail's Puls has reported this figure since
    it was built; the list never did, which is why he was looking at the one
    screen that knew and did not say."""
    _job(con, "Wartende GmbH", score=None, external="a")
    _job(con, "Versteckte GmbH", score=None, external="b")
    db.hide_company(con, "Versteckte GmbH")
    con.commit()
    assert jobs._load_jobs("offen", 0, min_score=0)["pending_scores"] == \
        db.count_unscored_jobs(con) == 1


def test_a_posting_arriving_now_is_counted_before_it_is_graded(con):
    """The window this slice is about, end to end: it arrives, it is counted
    as waiting, it is graded, the count goes away."""
    assert jobs.scoring_line(db.count_unscored_jobs(con), True) == ""
    job_id = _job(con, "Frische GmbH", score=None, external="a")
    assert jobs.scoring_line(db.count_unscored_jobs(con), True) == \
        "1 Anzeige wartet noch auf die Bewertung."
    db.set_job_score(con, job_id, 68, "weil")
    con.commit()
    assert jobs.scoring_line(db.count_unscored_jobs(con), True) == ""
