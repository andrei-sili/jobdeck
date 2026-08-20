"""Companies he never wants to see again.

He pressed "not interested" three times on one staffing agency, because each
press only ever reached one advert. These tests are about the two ways hiding
an employer can go wrong: reaching the wrong postings, and reaching too few —
a spelling variant, or an advert that did not exist yet when he pressed.
"""

import pytest

from jobdeck import db
from jobdeck.dedupe import norm


def _job(con, company, *, title="Entwickler", score=70, external="", **extra):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": external or f"e{company}{title}",
        "title": title, "company": company,
        "url": "https://example.invalid/1", **extra,
    })
    if score is not None:
        db.set_job_score(con, job_id, score, "weil")
    con.commit()
    return job_id


def _open_ids(con, **kw):
    return [r["id"] for r in db.list_jobs(con, status="new", **kw)]


# ------------------------------------------------------------------ the key


def test_hiding_a_company_uses_the_key_the_rest_of_the_app_groups_by(con):
    """The one differential that matters. Three keys must be the same key:
    what the list GROUPS by, what the send gate refuses a second application
    on, and what hiding files under. If they ever drift, he hides a company
    and its postings stay — or worse, someone else's go."""
    decorated = _job(con, "Beispiel® GmbH", external="a")
    plain = _job(con, "Beispiel GmbH", external="b")

    key = db.hide_company(con, "Beispiel GmbH")

    assert key == norm("Beispiel® GmbH"), "the ® would be a second company"
    assert _open_ids(con, hidden="exclude") == []
    assert sorted(_open_ids(con, hidden="only")) == sorted([decorated, plain])


def test_a_posting_with_no_company_cannot_be_hidden_as_one(con):
    """A blank field is missing information, not an employer. Hiding
    "everything with no name" would take out rows with nothing in common —
    which is why `_COMPANY_KEY_SQL` gives such a posting its own key."""
    nameless = _job(con, "", external="a")
    named = _job(con, "Echte GmbH", external="b")

    assert db.hide_company(con, "") == ""
    assert db.hide_company(con, "   ") == ""
    assert sorted(_open_ids(con, hidden="exclude")) == sorted([nameless, named])


def test_an_empty_row_in_the_table_hides_nothing(con):
    """Belt and braces on the query itself: `jd_norm('') IN (…)` must not
    match a nameless posting even if a blank key got into the table."""
    nameless = _job(con, "", external="a")
    con.execute("INSERT INTO hidden_companies (company_key, company, hidden_at)"
                " VALUES ('', '', '2026-08-20T10:00:00')")
    con.commit()

    assert _open_ids(con, hidden="exclude") == [nameless]


# --------------------------------------------------------------- what it hides


def test_hiding_reaches_every_advert_of_that_company(con):
    """The complaint, exactly: one press, all of them."""
    ids = [_job(con, "Zeitarbeit GmbH", title=f"Rolle {n}", external=f"z{n}")
           for n in range(4)]
    other = _job(con, "Andere GmbH", external="x")

    db.hide_company(con, "Zeitarbeit GmbH")

    assert _open_ids(con, hidden="exclude") == [other]
    assert sorted(_open_ids(con, hidden="only")) == sorted(ids)


def test_hiding_reaches_adverts_that_did_not_exist_yet(con):
    """The reason this is a table and not a column on `jobs`. A column would
    have to be written at every future insert by something that remembered to;
    the filter is read-time, so tomorrow's advert is already hidden."""
    db.hide_company(con, "Zeitarbeit GmbH")

    _job(con, "Zeitarbeit GmbH", title="Morgen ausgeschrieben", external="neu")

    assert _open_ids(con, hidden="exclude") == []


def test_taking_a_company_back_brings_every_advert_with_it(con):
    """Nothing was ever written to the postings, so nothing has to be undone —
    including the ones discovered while it was hidden."""
    before = _job(con, "Zeitarbeit GmbH", external="a")
    key = db.hide_company(con, "Zeitarbeit GmbH")
    during = _job(con, "Zeitarbeit GmbH", title="Zweite", external="b")

    db.unhide_company(con, key)

    assert sorted(_open_ids(con, hidden="exclude")) == sorted([before, during])


def test_pressing_twice_is_not_a_second_decision(con):
    _job(con, "Zeitarbeit GmbH")
    db.hide_company(con, "Zeitarbeit GmbH")
    first = db.list_hidden_companies(con)[0]["hidden_at"]

    db.hide_company(con, "Zeitarbeit GmbH")

    rows = db.list_hidden_companies(con)
    assert len(rows) == 1
    assert rows[0]["hidden_at"] == first


def test_hiding_never_touches_the_postings_themselves(con):
    """It is a view, not a status. `x` used to write `status='skipped'`, which
    is a fact about the posting; this is a fact about him."""
    job_id = _job(con, "Zeitarbeit GmbH")
    before = dict(db.get_job(con, job_id))

    db.hide_company(con, "Zeitarbeit GmbH")

    assert dict(db.get_job(con, job_id)) == before


# ------------------------------------------------- what the screen needs to say


def test_the_list_of_hidden_companies_says_how_much_it_is_hiding(con):
    """Taking a company back has to be a decision, not a guess."""
    for n in range(3):
        _job(con, "Zeitarbeit GmbH", title=f"R{n}", external=f"z{n}")
    _job(con, "Klein GmbH", external="k")
    db.hide_company(con, "Zeitarbeit GmbH")
    db.hide_company(con, "Klein GmbH")

    rows = db.list_hidden_companies(con)

    assert [r["company"] for r in rows] == ["Klein GmbH", "Zeitarbeit GmbH"]
    assert {r["company"]: r["hidden_jobs"] for r in rows} == \
        {"Zeitarbeit GmbH": 3, "Klein GmbH": 1}
    assert db.count_hidden_companies(con) == 2


def test_the_name_on_screen_is_the_spelling_he_saw(con):
    """The key is normalised and unreadable; a view listing "beispiel gmbh"
    would not be a list of his decisions."""
    db.hide_company(con, "Beispiel® GmbH")

    assert db.list_hidden_companies(con)[0]["company"] == "Beispiel® GmbH"


# ---------------------------------------------------------------- the signature


def test_the_pipeline_notices_a_company_being_hidden(con):
    """Hiding removes rows from every list on every pipeline page, and it is
    not a write to `jobs`. Without a term here the list he just pruned keeps
    showing the company until something unrelated happens to change."""
    _job(con, "Zeitarbeit GmbH")
    before = db.data_signature(con)

    db.hide_company(con, "Zeitarbeit GmbH")

    assert db.data_signature(con) != before


def test_swapping_one_hidden_company_for_another_is_a_change(con):
    """The case a timestamp cannot see, and the reason the term counts rowids.

    Take one company back and hide a different one inside the same second: the
    COUNT returns to what it was and `MAX(hidden_at)` is the same second, so a
    timestamp-keyed term compares EQUAL — and the list keeps hiding the company
    he just released while showing the one he just hid."""
    _job(con, "Alpha GmbH", external="a")
    _job(con, "Beta GmbH", external="b")
    _job(con, "Gamma GmbH", external="c")
    first = db.hide_company(con, "Alpha GmbH")
    db.hide_company(con, "Beta GmbH")
    before = db.data_signature(con)

    db.unhide_company(con, first)
    db.hide_company(con, "Gamma GmbH")

    # Not asserted on the clock: two writes landing in the same second is a
    # race, and a test that needs one is a test that fails at midnight. The
    # property is that the term does not rest on the timestamp at all —
    # `test_releasing_the_newest_hidden_company_is_a_change` is the pair a
    # rowid cannot see, and this is the pair a timestamp cannot.
    assert db.count_hidden_companies(con) == 2, "same count, on purpose"
    assert db.data_signature(con) != before


# ------------------------------------------------------- every query, one truth


@pytest.mark.parametrize("call", [
    lambda con, **kw: len(db.list_jobs(con, status="new", **kw)),
    lambda con, **kw: db.count_jobs(con, status="new", **kw),
    lambda con, **kw: len(db.list_job_groups(con, status="new", **kw)),
    lambda con, **kw: db.count_job_groups(con, status="new", **kw),
])
def test_every_query_agrees_about_what_is_hidden(con, call):
    """The page prints a total beside a list. If one of these forgot the arm,
    the count and the rows would describe different corpora — and the pager
    would offer a page that renders empty."""
    _job(con, "Zeitarbeit GmbH", external="a")
    _job(con, "Andere GmbH", external="b")

    assert call(con) == 2
    db.hide_company(con, "Zeitarbeit GmbH")
    assert call(con, hidden="exclude") == 1
    assert call(con, hidden="only") == 1


def test_a_hidden_company_cannot_leak_back_as_a_sibling(con):
    """A grouped row lists the OTHER postings of its company, through its own
    query. That query has already leaked a hidden pile once."""
    keep = _job(con, "Andere GmbH", title="Beste", score=90, external="a")
    _job(con, "Andere GmbH", title="Zweite", score=80, external="b")
    _job(con, "Zeitarbeit GmbH", title="Dritte", score=85, external="c")
    db.hide_company(con, "Zeitarbeit GmbH")

    groups = db.list_job_groups(con, status="new", hidden="exclude")
    keys = [r["company_key"] for r in groups]
    siblings = db.list_company_siblings(con, keys, status="new",
                                        hidden="exclude")

    assert [r["id"] for r in groups] == [keep]
    assert siblings, "the sibling query returned nothing to check"
    assert all(r["company"] == "Andere GmbH" for r in siblings)


def test_an_unknown_filter_word_is_refused_rather_than_ignored(con):
    """Falling through would SHOW a pile the caller asked to hide."""
    with pytest.raises(ValueError, match="hidden"):
        db.list_jobs(con, status="new", hidden="vielleicht")


def test_releasing_the_newest_hidden_company_is_a_change(con):
    """The pair the undo bar actually produces, and the one a bare rowid
    cannot see: a rowid table without AUTOINCREMENT reuses the number freed by
    a DELETE, so hide A, release A, hide B all land on rowid 1 — and the list
    would stay pruned of the company he just released."""
    _job(con, "Alpha GmbH", external="a")
    _job(con, "Beta GmbH", external="b")
    key = db.hide_company(con, "Alpha GmbH")
    before = db.data_signature(con)

    db.unhide_company(con, key)
    db.hide_company(con, "Beta GmbH")

    assert db.count_hidden_companies(con) == 1, "same count, on purpose"
    assert db.data_signature(con) != before


def test_a_company_with_nothing_open_still_lists_what_it_hides(con):
    """`list_hidden_companies` counts every advert of the company, not only
    the ones on 'new' — the view exists to undo a decision, and a company
    whose adverts are all applied to or set aside is still hidden."""
    job_id = _job(con, "Alt GmbH")
    db.set_job_status(con, job_id, "applied")
    con.commit()
    db.hide_company(con, "Alt GmbH")

    assert db.list_hidden_companies(con)[0]["hidden_jobs"] == 1


# ------------------------------------------------ what it must also reach


def test_a_hidden_company_never_costs_him_a_paid_draft(con, data_dir):
    """Hiding is read-time, and the prepare batch is a WRITE path that spends
    about nine cents a letter. Without the arm it would go on writing them for
    a company he will never apply to."""
    keep = _job(con, "Andere GmbH", external="a", score=90)
    _job(con, "Zeitarbeit GmbH", external="b", score=95)
    con.execute("UPDATE jobs SET published_on=date('now'), contact_email='x@y.de'")
    con.commit()

    db.hide_company(con, "Zeitarbeit GmbH")

    picked = db.jobs_to_prepare(con, limit=10, max_age_days=45, min_score=50)
    assert [r["id"] for r in picked] == [keep]


def test_a_hidden_company_never_costs_him_a_scoring_call(con, data_dir):
    """Scoring is haiku, about half a cent an advert, and a staffing agency
    posts continuously. The backlog is counted the way the batch selects, or
    the Puls pulses for ever over adverts nothing will score."""
    keep = _job(con, "Andere GmbH", external="a", score=None)
    _job(con, "Zeitarbeit GmbH", external="b", score=None)
    con.commit()
    assert db.count_unscored_jobs(con) == 2

    db.hide_company(con, "Zeitarbeit GmbH")

    assert [r["id"] for r in db.list_unscored_jobs(con)] == [keep]
    assert db.count_unscored_jobs(con) == 1


def test_the_price_named_in_the_undo_bar_is_the_real_one(con):
    """It used to be read out of a 1000-row page of `list_jobs` ordered by
    DATE and filtered in Python — a cap applied to his 1080 open postings, so
    the "best" was the best of whatever the cap happened to keep."""
    for n in range(3):
        _job(con, "Zeitarbeit GmbH", external=f"z{n}", score=40 + n * 10)
    _job(con, "Andere GmbH", external="x", score=99)

    cost = db.company_cost(con, "Zeitarbeit GmbH")

    assert cost == {"jobs": 3, "best": 60}
    assert db.company_cost(con, "") == {"jobs": 0, "best": 0}
    assert db.company_cost(con, "Gibt Es Nicht GmbH") == {"jobs": 0, "best": 0}


def test_the_app_can_tell_whether_a_company_is_already_hidden(con):
    """Pressing `x` inside the pile must not announce a hide that did not
    happen — nor offer an undo that would REVEAL the company."""
    _job(con, "Zeitarbeit GmbH")

    assert db.is_company_hidden(con, "Zeitarbeit GmbH") is False
    db.hide_company(con, "Zeitarbeit GmbH")
    assert db.is_company_hidden(con, "Zeitarbeit GmbH") is True
    # …through the same key, so a spelling variant answers the same
    assert db.is_company_hidden(con, "zeitarbeit  gmbh") is True
    assert db.is_company_hidden(con, "") is False
