"""Two orders that look like one.

`_JOB_ORDER_SQL` was used twice: as the list's ORDER BY, and inside the WINDOW
that picks which posting REPRESENTS a company. Letting his sort control reach
the second would mean that switching to "newest first" quietly re-elected the
newest advert of a nineteen-advert staffing agency to speak for it — a ranking
regression invisible from the screen, because the row still looks like a
company.
"""

import datetime

import pytest

from jobdeck import db


def _job(con, ext, company, score, days_old):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": ext, "title": f"Rolle {ext}",
        "company": company, "url": f"https://example.invalid/{ext}",
    })
    day = (datetime.date.today() - datetime.timedelta(days=days_old)).isoformat()
    con.execute("UPDATE jobs SET published_on=? WHERE id=?", (day, job_id))
    db.set_job_score(con, job_id, score, "weil")
    con.commit()
    return job_id


def test_the_sort_never_changes_which_posting_speaks_for_a_company(con):
    """The row stands for the whole company, so it has to be the best one it
    holds — whichever way he has asked the list to be ordered."""
    best = _job(con, "a", "Zeitarbeit GmbH", 90, days_old=30)
    _job(con, "b", "Zeitarbeit GmbH", 40, days_old=0)      # newest, but weak

    by_score = db.list_job_groups(con, status="new", sort="score")
    by_date = db.list_job_groups(con, status="new", sort="date")

    assert [r["id"] for r in by_score] == [best]
    assert [r["id"] for r in by_date] == [best], \
        "newest-first re-elected the weakest advert to represent the company"
    assert by_date[0]["company_count"] == 2


def test_the_sort_does_change_the_order_of_the_companies(con):
    """…while still doing the thing it exists for."""
    older_strong = _job(con, "a", "Alpha GmbH", 90, days_old=20)
    newer_weak = _job(con, "b", "Beta GmbH", 45, days_old=1)

    assert [r["id"] for r in db.list_job_groups(con, status="new", sort="score")] \
        == [older_strong, newer_weak]
    assert [r["id"] for r in db.list_job_groups(con, status="new", sort="date")] \
        == [newer_weak, older_strong]


def test_a_posting_with_no_date_sorts_last_rather_than_oldest(con):
    """An unknown date is stored as the empty string, not as NULL. Without
    NULLIF it would sort as if it were the oldest possible date — the app would
    be asserting an age it does not have."""
    dated = _job(con, "a", "Alpha GmbH", 50, days_old=400)
    undated = _job(con, "b", "Beta GmbH", 95, days_old=0)
    con.execute("UPDATE jobs SET published_on='' WHERE id=?", (undated,))
    con.commit()

    assert [r["id"] for r in db.list_jobs(con, status="new", sort="date")] \
        == [dated, undated]


def test_each_order_keeps_the_other_as_its_tie_break(con):
    """Neither is a pure one-dimensional sort. With 222 of 300 rows under 40
    points, newest-first alone would put fresh noise at the top — which is the
    complaint it is meant to answer."""
    same_day = 3
    strong = _job(con, "a", "Alpha GmbH", 90, days_old=same_day)
    weak = _job(con, "b", "Beta GmbH", 20, days_old=same_day)

    assert [r["id"] for r in db.list_jobs(con, status="new", sort="date")] \
        == [strong, weak]


@pytest.mark.parametrize("name", ["", "kaputt", None, "Score", "id"])
def test_an_unknown_sort_name_falls_back_rather_than_raising(con, name):
    """The name reaches here from a stored setting he can edit; an unknown one
    would otherwise be a screen that will not open."""
    assert db.list_order_sql(name) == db.LIST_ORDERS[db.DEFAULT_LIST_ORDER]


def test_the_default_is_newest_first(con):
    """His words: filters that work — on score, DEFAULT on publication date."""
    assert db.DEFAULT_LIST_ORDER == "date"
    older = _job(con, "a", "Alpha GmbH", 90, days_old=20)
    newer = _job(con, "b", "Beta GmbH", 45, days_old=1)

    assert [r["id"] for r in db.list_jobs(con, status="new")] == [newer, older]


def test_a_company_is_ranked_by_its_newest_advert_not_its_best_ones_date(con):
    """A row stands for a company's BEST advert. Ordering the LIST by that
    advert's date puts a company that posted today at thirty days old because
    its strongest advert is that old — which is not what "Neueste zuerst"
    means to anyone reading it."""
    _job(con, "a", "Alpha GmbH", 90, days_old=30)   # best, but old
    _job(con, "b", "Alpha GmbH", 40, days_old=0)    # newest
    _job(con, "c", "Beta GmbH", 60, days_old=10)

    by_date = db.list_job_groups(con, status="new", sort="date")

    assert [r["company"] for r in by_date] == ["Alpha GmbH", "Beta GmbH"]
    # …and the row is still the company's BEST advert, not its newest
    assert by_date[0]["match_score"] == 90


def test_the_score_order_still_ranks_companies_by_their_best(con):
    _job(con, "a", "Alpha GmbH", 40, days_old=0)
    _job(con, "b", "Beta GmbH", 90, days_old=30)

    by_score = db.list_job_groups(con, status="new", sort="score")

    assert [r["company"] for r in by_score] == ["Beta GmbH", "Alpha GmbH"]


def test_the_floor_reaches_the_sibling_query_too(con):
    """A grouped row lists the OTHER adverts of its company through its own
    query. Without the floor there, a company row respecting "ab 60" would
    unfold to a list of adverts scoring 20."""
    _job(con, "a", "Alpha GmbH", 90, days_old=1)
    _job(con, "b", "Alpha GmbH", 80, days_old=2)
    weak = _job(con, "c", "Alpha GmbH", 20, days_old=3)

    groups = db.list_job_groups(con, status="new", min_score=60)
    keys = [r["company_key"] for r in groups]
    siblings = db.list_company_siblings(con, keys, status="new", min_score=60)

    assert weak not in [r["id"] for r in siblings]
    assert len(siblings) == 1
    # …and with the floor off it is there, so the test is not passing on an
    # empty sibling list
    assert len(db.list_company_siblings(con, keys, status="new")) == 2
