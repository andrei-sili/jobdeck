"""Age as a dimension of the ordering — and the two implementations of it.

The ordering must happen in SQL, because it decides which rows a page contains;
the label beside the score is rendered from the same numbers. So the Python rule
and its SQL mirror are one contract, and the differential test here is what
proves they are equal.
"""

import datetime
import sqlite3

import pytest

from jobdeck import db, freshness


@pytest.mark.parametrize("age, expected", [
    (0, 0), (1, 0), (13, 0),          # fresh: nothing to pay
    (14, 5), (29, 5),
    (30, 12), (59, 12),
    (60, 20), (365, 20),
    (-3, 0),                          # a date in the future is not stale
    (None, 0),                        # unknown: absence is not evidence
])
def test_the_penalty_is_a_step_function_with_no_gaps(age, expected):
    assert freshness.penalty(age) == expected


def test_a_stale_high_score_falls_below_a_fresh_lower_one():
    """The complaint this exists for: a 92 from March outranked a 78 from
    yesterday, and the best-looking rows were the likeliest to be gone."""
    assert freshness.effective_score(92, 150) == 72
    assert freshness.effective_score(78, 1) == 78
    assert freshness.effective_score(92, 150) < freshness.effective_score(78, 1)


def test_age_can_never_manufacture_a_mismatch():
    # 0 means "violates a hard requirement" (ai/scoring.py). Nothing else may
    # reach it, or the inbox would hide a posting for being old.
    assert freshness.effective_score(1, 9999) == 1
    assert freshness.effective_score(5, 9999) == 1
    assert freshness.effective_score(0, 0) == 0      # a mismatch stays a mismatch
    assert freshness.effective_score(0, 9999) == 0
    assert freshness.effective_score(None, 3) is None


def _sql(expr: str, **params):
    con = sqlite3.connect(":memory:")
    try:
        return con.execute(f"SELECT {expr}", params).fetchone()[0]
    finally:
        con.close()


def test_the_sql_mirror_matches_the_python_rule_for_every_age_and_score():
    expr = freshness.effective_score_sql(score_expr=":score", age_expr=":age")
    divergences = []
    for age in [None, *range(-10, 400)]:
        for score in (None, 0, 1, 5, 12, 20, 50, 78, 92, 100):
            in_sql = _sql(expr, score=score, age=age)
            in_python = freshness.effective_score(score, age)
            if in_sql != in_python:
                divergences.append((age, score, in_sql, in_python))
    assert divergences == []


def test_the_sql_age_expression_counts_whole_days_and_tolerates_no_date():
    expr = f"{freshness.AGE_SQL}"
    today = datetime.date.today()
    for days in (0, 1, 14, 400):
        published = (today - datetime.timedelta(days=days)).isoformat()
        measured = _sql(expr.replace("published_on", f"'{published}'"))
        # UTC 'now' against a local date: one day of slack, never more
        assert abs(measured - days) <= 1, (days, measured)
    assert _sql(expr.replace("published_on", "''")) is None
    assert _sql(expr.replace("published_on", "'irgendwann'")) is None


def _job(con, ext, score, published_on):
    job_id = db.insert_job_if_new(con, {
        "source": "arbeitsagentur", "external_id": ext, "title": "Dev",
        "company": "Firma", "url": f"https://firma.de/{ext}",
    })
    con.execute("UPDATE jobs SET match_score=?, published_on=? WHERE id=?",
                (score, published_on, job_id))
    return job_id


def test_the_inbox_orders_on_the_aged_score_and_reports_it(con, data_dir):
    today = datetime.date.today()

    def ago(days):
        return (today - datetime.timedelta(days=days)).isoformat()

    stale_star = _job(con, "march-92", 92, ago(150))
    fresh_good = _job(con, "yesterday-78", 78, ago(1))
    undated = _job(con, "no-date-80", 80, "")
    con.commit()

    rows = db.list_jobs(con, status="new")
    assert [r["id"] for r in rows] == [undated, fresh_good, stale_star]
    by_id = {r["id"]: r for r in rows}
    assert by_id[stale_star]["effective_score"] == 72
    assert by_id[stale_star]["age_days"] in (149, 150, 151)
    assert by_id[fresh_good]["effective_score"] == 78
    # an unknown date is not penalised, so an 80 keeps its 80
    assert by_id[undated]["effective_score"] == 80
    assert by_id[undated]["age_days"] is None


def test_a_known_date_wins_the_tie_over_an_unknown_one(con, data_dir):
    today = datetime.date.today().isoformat()
    dated = _job(con, "dated", 80, today)
    undated = _job(con, "undated", 80, "")
    con.commit()
    assert [r["id"] for r in db.list_jobs(con, status="new")] == [dated, undated]
