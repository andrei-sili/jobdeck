"""The floor he sets under the list.

Measured on his corpus the day this was built: his working list holds 300
postings, and 222 of them score under 40 — three quarters of what he scrolls is
noise the machine has already graded as weak. These tests are about the three
ways a floor can lie: comparing a different number from the one on screen,
swallowing a posting nobody has judged yet, and letting the knock-out pile in.
"""

import datetime

import pytest

from jobdeck import db, freshness


def _job(con, score, *, published=None, external="e", company="Firma GmbH"):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": external, "title": "Entwickler",
        "company": company, "url": "https://example.invalid/1",
    })
    # `published_on` is DERIVED from published_at at insert, so it is set here
    # the way the rest of the suite sets it.
    con.execute("UPDATE jobs SET published_on=? WHERE id=?",
                (published or datetime.date.today().isoformat(), job_id))
    if score is not None:
        db.set_job_score(con, job_id, score, "weil")
    con.commit()
    return job_id


def _ids(con, floor):
    return [r["id"] for r in db.list_jobs(con, status="new", min_score=floor)]


def test_the_floor_compares_the_number_the_row_prints(con):
    """A posting scored 65 but three months old prints its AGED score. If the
    floor compared the raw one, a row reading "55" would sit above a floor of
    60 and the two figures on the same screen would disagree."""
    old_day = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    stale = _job(con, 65, published=old_day, external="a")
    fresh = _job(con, 65, external="b")

    shown = {r["id"]: r["effective_score"]
             for r in db.list_jobs(con, status="new")}
    assert shown[stale] < 60 <= shown[fresh], "the fixture proves nothing"

    assert _ids(con, 60) == [fresh]
    assert sorted(_ids(con, 0)) == sorted([stale, fresh])


def test_a_posting_nobody_has_judged_yet_is_never_swallowed(con):
    """`NULL >= 40` is NULL. Without the explicit arm, a posting that arrived
    between a search and the scoring pass disappears with no pile to find it
    in and no number beside it saying why."""
    unscored = _job(con, None, external="a")
    weak = _job(con, 20, external="b")

    assert _ids(con, 40) == [unscored]
    assert sorted(_ids(con, 0)) == sorted([unscored, weak])


def test_the_knock_out_pile_does_not_come_back_through_the_floor(con):
    """0 is not a low score — it means the posting violates a hard requirement
    and it has its own named pile. On his corpus that is 564 postings."""
    knocked = _job(con, 0, external="a")
    weak = _job(con, 5, external="b")

    # A floor of 1 — the lowest a real score can be — must not readmit it
    assert _ids(con, 1) == [weak]
    # …and the pile stays governed by its own filter, not by this one
    assert [r["id"] for r in db.list_jobs(con, status="new", min_score=0,
                                          mismatches="exclude")] == [weak]
    assert [r["id"] for r in db.list_jobs(con, status="new", min_score=0,
                                          mismatches="only")] == [knocked]


def test_a_floor_of_zero_is_the_filter_switched_off(con):
    """He turns it off from the same control that sets it; there is no second
    switch to get out of step with the number."""
    ids = [_job(con, n, external=f"e{n}") for n in (5, 45, 85)]

    assert sorted(_ids(con, 0)) == sorted(ids)


@pytest.mark.parametrize("floor,expected", [(0, 3), (40, 2), (60, 1), (90, 0)])
def test_the_floor_cuts_where_it_says_it_cuts(con, floor, expected):
    for n in (30, 55, 85):
        _job(con, n, external=f"e{n}")

    assert len(_ids(con, floor)) == expected


def test_the_floor_and_the_age_pile_bind_their_values_in_order(con):
    """Both clauses carry a bound value and the params list is positional, so
    a clause appended without its binding — or in the wrong order — silently
    shifts the other one. This is the pair that would catch it."""
    old_day = (datetime.date.today() - datetime.timedelta(days=200)).isoformat()
    fresh_strong = _job(con, 85, external="a")
    _job(con, 85, published=old_day, external="b")
    _job(con, 10, external="c")

    rows = db.list_jobs(con, status="new", min_score=40, old="exclude",
                        stale_age_days=30)

    assert [r["id"] for r in rows] == [fresh_strong]


@pytest.mark.parametrize("call", [
    lambda con, **kw: len(db.list_jobs(con, status="new", **kw)),
    lambda con, **kw: db.count_jobs(con, status="new", **kw),
    lambda con, **kw: len(db.list_job_groups(con, status="new", **kw)),
    lambda con, **kw: db.count_job_groups(con, status="new", **kw),
])
def test_every_query_cuts_at_the_same_place(con, call):
    """The page prints a total beside its list. One query forgetting the floor
    would offer a page of rows that renders empty."""
    _job(con, 85, external="a", company="Eine GmbH")
    _job(con, 20, external="b", company="Andere GmbH")

    assert call(con, min_score=0) == 2
    assert call(con, min_score=40) == 1


def test_the_floor_agrees_with_the_python_that_computes_the_same_number(con):
    """`effective_score` exists twice — once in Python for the tests and the
    screen, once as SQL for the queries. A differential, because the floor is
    the first place they are asked to agree at a boundary."""
    old_day = (datetime.date.today() - datetime.timedelta(days=45)).isoformat()
    _job(con, 70, published=old_day, external="a")

    row = db.list_jobs(con, status="new")[0]
    in_python = freshness.effective_score(row["match_score"], row["age_days"])

    assert row["effective_score"] == in_python
    assert _ids(con, in_python) == [row["id"]]
    assert _ids(con, in_python + 1) == []
