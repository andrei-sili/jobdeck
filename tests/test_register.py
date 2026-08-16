"""What the Bewerbungen screen states, pinned before anything is drawn.

The two claims worth guarding are the two the real register forced: that the
pipeline is NOT a chain of subsets, and that the app does not take credit for
the 44 applications that predate it.
"""

import datetime

import pytest

from jobdeck import db
from jobdeck.services import register
from jobdeck.services.register import Share

TODAY = datetime.date(2026, 8, 16)


def _view(**over):
    """A pipeline reading with every population named, so a test can move one."""
    view = {"jobs_total": 100, "scored_above_zero": 60, "scored_zero": 40,
            "opened": 20, "drafted": 10, "drafted_unread": 0, "applied": 5,
            "applied_without_letter": 0, "apps": []}
    view.update(over)
    return view


def _app(status="Gesendet", gesendet_am="2026-08-01", firma="Firma GmbH",
         kanal="E-Mail", row_id=1):
    return {"id": row_id, "status": status, "gesendet_am": gesendet_am,
            "firma": firma, "kanal": kanal}


# --------------------------------------------------------------------------
# The pipeline, and the step that is not a subset
# --------------------------------------------------------------------------
def test_the_step_that_is_not_a_subset_says_so_itself():
    """45 of his postings have a letter and only 33 were ever opened: the
    daily batch and the form flow both write one without the ad being read.
    A column drawn as nested subsets would simply be false, so the break is
    stated ON the step where it happens, not in a caption further away."""
    steps = {step.key: step for step in register.pipeline(
        _view(opened=20, drafted=30, drafted_unread=25))}

    assert "25" in steps["anschreiben"].note
    assert steps["anschreiben"].count > steps["angesehen"].count


def test_a_pipeline_that_really_does_nest_makes_no_excuse():
    steps = {step.key: step for step in register.pipeline(
        _view(drafted=10, drafted_unread=0))}

    assert steps["anschreiben"].note == ""


def test_every_bar_is_measured_against_what_was_found():
    steps = register.pipeline(_view(jobs_total=100, opened=20))

    assert steps[0].share == 1.0
    assert next(s for s in steps if s.key == "angesehen").share == 0.2


def test_an_empty_pipeline_states_zero_rather_than_dividing_by_it():
    steps = register.pipeline(_view(jobs_total=0, scored_above_zero=0,
                                    scored_zero=0, opened=0, drafted=0,
                                    applied=0))

    assert [step.share for step in steps] == [0.0] * len(steps)


# --------------------------------------------------------------------------
# The register is older than the app
# --------------------------------------------------------------------------
def test_the_register_names_what_this_app_did_not_do():
    """44 of his 76 applications came out of the tracker he kept before
    JobDeck existed. Calling all 76 "gesendet" would be this screen taking
    credit for them."""
    apps = [_app(row_id=n) for n in range(10)]

    steps = {step.key: step for step in register.ledger(
        _view(applied=3, apps=apps))}

    assert steps["register"].count == 10
    assert "3 über JobDeck" in steps["register"].note
    assert "7 von Hand" in steps["register"].note


def test_a_register_this_app_built_entirely_makes_no_such_claim():
    apps = [_app(row_id=n) for n in range(3)]

    steps = {step.key: step for step in register.ledger(
        _view(applied=3, apps=apps))}

    assert steps["register"].note == ""


def test_answered_and_open_are_counted_by_the_shared_vocabulary():
    apps = [_app(status="Gesendet", row_id=1), _app(status="Absage", row_id=2),
            _app(status="Einladung", row_id=3),
            _app(status="In Bearbeitung", row_id=4)]

    steps = {step.key: step for step in register.ledger(
        _view(applied=4, apps=apps))}

    assert steps["beantwortet"].count == 2
    assert steps["offen"].count == 2


# --------------------------------------------------------------------------
# The rhythm strip
# --------------------------------------------------------------------------
def test_the_strip_covers_the_window_and_ends_today():
    strip = register.rhythm([], TODAY, days=60)

    assert len(strip) == 60
    assert strip[-1].date == TODAY
    assert strip[0].date == TODAY - datetime.timedelta(days=59)


def test_a_day_counts_every_application_sent_on_it():
    apps = [_app(gesendet_am="2026-08-11", row_id=1),
            _app(gesendet_am="2026-08-11", row_id=2),
            _app(gesendet_am="2026-08-16", row_id=3)]

    strip = {day.date: day.count for day in register.rhythm(apps, TODAY)}

    assert strip[datetime.date(2026, 8, 11)] == 2
    assert strip[TODAY] == 1
    assert strip[datetime.date(2026, 8, 12)] == 0


def test_an_application_older_than_the_window_moves_no_column():
    apps = [_app(gesendet_am="2026-01-01")]

    assert all(day.count == 0 for day in register.rhythm(apps, TODAY))


def test_an_unreadable_date_moves_no_column_either():
    apps = [_app(gesendet_am=""), _app(gesendet_am="irgendwann", row_id=2)]

    assert all(day.count == 0 for day in register.rhythm(apps, TODAY))


def test_the_busiest_day_is_reported_so_the_scale_is_stated():
    apps = [_app(gesendet_am="2026-08-11", row_id=n) for n in range(8)]
    apps += [_app(gesendet_am="2026-08-16", row_id=99)]

    peak = register.busiest(register.rhythm(apps, TODAY))

    assert peak.count == 8
    assert peak.date == datetime.date(2026, 8, 11)


def test_a_window_with_nothing_in_it_has_no_busiest_day():
    assert register.busiest(register.rhythm([], TODAY)) is None


def test_the_pause_is_measured_between_two_working_days():
    apps = [_app(gesendet_am="2026-07-01", row_id=1),
            _app(gesendet_am="2026-08-06", row_id=2)]

    assert register.longest_pause(register.rhythm(apps, TODAY)) == 35


def test_todays_quiet_is_the_present_not_a_pause():
    """A trailing run of empty days is simply now. Counting it would tell him
    he had stopped working because he has not sent one YET today."""
    apps = [_app(gesendet_am="2026-07-01", row_id=1),
            _app(gesendet_am="2026-07-02", row_id=2)]

    assert register.longest_pause(register.rhythm(apps, TODAY)) == 0


def test_one_single_day_of_work_is_not_a_pause_either():
    apps = [_app(gesendet_am="2026-08-01")]

    assert register.longest_pause(register.rhythm(apps, TODAY)) == 0


# --------------------------------------------------------------------------
# Who is silent, and since when
# --------------------------------------------------------------------------
def test_only_an_unanswered_application_is_silent():
    apps = [_app(status="Gesendet", firma="Offen GmbH", row_id=1),
            _app(status="Absage", firma="Abgesagt GmbH", row_id=2),
            _app(status="Einladung", firma="Eingeladen GmbH", row_id=3)]

    names = [row.firma for row in register.silence(apps, 14, TODAY)]

    assert names == ["Offen GmbH"]


def test_the_longest_silence_leads():
    apps = [_app(gesendet_am="2026-08-14", firma="Neu GmbH", row_id=1),
            _app(gesendet_am="2026-06-10", firma="Alt GmbH", row_id=2),
            _app(gesendet_am="2026-08-01", firma="Mittel GmbH", row_id=3)]

    rows = register.silence(apps, 14, TODAY)

    assert [row.firma for row in rows] == ["Alt GmbH", "Mittel GmbH", "Neu GmbH"]
    assert [row.days for row in rows] == [67, 15, 2]


def test_the_threshold_decides_which_row_is_overdue():
    apps = [_app(gesendet_am="2026-08-02", row_id=1),
            _app(gesendet_am="2026-08-03", row_id=2)]

    rows = {row.bewerbung_id: row.overdue
            for row in register.silence(apps, 14, TODAY)}

    assert rows == {1: True, 2: False}


def test_a_row_with_no_readable_date_keeps_its_place_and_claims_nothing():
    """Imported rows exist with no date at all. Dropping one hides a real
    application; dating it to today sorts it among this morning's.

    The row sent TODAY is what makes this a real assertion: an unknown age
    read as zero would tie with it, and the list would then be ordered by
    whatever came out of the database first."""
    apps = [_app(gesendet_am="", firma="Ohne Datum GmbH", row_id=1),
            _app(gesendet_am=TODAY.isoformat(), firma="Heute GmbH", row_id=2),
            _app(gesendet_am="2026-08-14", firma="Mit Datum GmbH", row_id=3)]

    rows = register.silence(apps, 14, TODAY)

    assert [row.firma for row in rows] == [
        "Mit Datum GmbH", "Heute GmbH", "Ohne Datum GmbH"]
    assert rows[-1].days is None
    assert rows[-1].overdue is False


# --------------------------------------------------------------------------
# What answers, and what each board brings
# --------------------------------------------------------------------------
def test_the_answered_share_is_counted_per_channel():
    apps = [_app(kanal="E-Mail", status="Absage", row_id=1),
            _app(kanal="E-Mail", status="Gesendet", row_id=2),
            _app(kanal="Online-Portal", status="Gesendet", row_id=3),
            _app(kanal="Online-Portal", status="Gesendet", row_id=4),
            _app(kanal="Online-Portal", status="Einladung", row_id=5)]

    shares = {share.label: share for share in register.by_channel(apps)}

    assert (shares["E-Mail"].part, shares["E-Mail"].whole) == (1, 2)
    assert (shares["Online-Portal"].part, shares["Online-Portal"].whole) == (1, 3)
    assert [s.label for s in register.by_channel(apps)][0] == "Online-Portal"


def test_a_handful_of_applications_may_not_be_stated_as_a_percentage():
    """A rate over four rows is arithmetic, not evidence."""
    few = [_app(row_id=n) for n in range(4)]
    many = [_app(row_id=n) for n in range(30)]

    assert not register.enough_for_a_rate(register.by_channel(few))
    assert register.enough_for_a_rate(register.by_channel(many))


def test_a_board_is_measured_by_what_its_postings_became():
    rows = [{"source": "arbeitnow", "jobs": 523, "applied": 12},
            {"source": "jooble", "jobs": 129, "applied": 5}]

    shares = register.by_source(rows)

    assert [s.label for s in shares] == ["arbeitnow", "jooble"]
    assert shares[0].ratio < shares[1].ratio, "most postings, fewest applications"


# --------------------------------------------------------------------------
# The counts themselves, against a database
# --------------------------------------------------------------------------
def _job(con, external_id, **over):
    values = {"source": "arbeitnow", "external_id": external_id,
              "title": "Entwickler", "company": f"Firma {external_id}",
              "url": f"https://x.example/{external_id}"}
    values.update(over)
    return db.insert_job_if_new(con, values)


def test_the_populations_are_counted_the_way_the_screen_reads_them(con):
    read = _job(con, "read")
    con.execute("UPDATE jobs SET opened_at='2026-08-16T10:00', match_score=80 "
                "WHERE id=?", (read,))
    batch = _job(con, "batch")   # a letter written without the ad being opened
    con.execute("UPDATE jobs SET match_score=70 WHERE id=?", (batch,))
    db.upsert_draft(con, batch, {"status": "ready",
                                 "anschreiben_body": "Sehr geehrte"})
    db.upsert_draft(con, read, {"status": "ready",
                                "anschreiben_body": "Sehr geehrte"})
    zero = _job(con, "zero")
    con.execute("UPDATE jobs SET match_score=0 WHERE id=?", (zero,))
    con.commit()

    counts = db.pipeline_counts(con)

    assert counts["jobs_total"] == 3
    assert counts["scored_above_zero"] == 2
    assert counts["scored_zero"] == 1
    assert counts["opened"] == 1
    assert counts["drafted"] == 2
    assert counts["drafted_unread"] == 1, "the batch wrote one unread"
    assert counts["applied"] == 0


def test_a_board_only_gets_credit_for_the_applications_it_carried(con):
    """An imported row has no posting and therefore no board. Folding those
    into a per-source figure would credit a board with work it never did."""
    job_id = _job(con, "one", source="jooble")
    db.apply_job(con, job_id, kanal="Online-Portal")
    db.add_bewerbung(con, {"firma": "Von Hand GmbH", "kanal": "E-Mail",
                           "status": "Gesendet"})
    con.commit()

    rows = {row["source"]: dict(row) for row in db.applications_by_source(con)}

    assert rows["jooble"]["jobs"] == 1
    assert rows["jooble"]["applied"] == 1
    assert sum(row["applied"] for row in rows.values()) == 1, "not the hand row"


# --------------------------------------------------------------------------
# A bar is a claim
# --------------------------------------------------------------------------
def test_a_rate_bar_is_the_rate_and_not_the_crowd_behind_it():
    """The answer panel drew the POPULATION while printing the RATE beside it,
    so 41 applications answered at 27 % out-drew 35 answered at 26 % — a
    visibly longer bar directly under a sentence saying the two are level."""
    shares = [Share("Online-Portal", 11, 41, 11 / 41),
              Share("E-Mail", 9, 35, 9 / 35)]

    widths = register.bar_widths(shares, "ratio")

    assert widths[0] == 1.0
    assert 0.95 < widths[1] < 1.0, "level rates must draw level bars"


def test_a_population_bar_still_shows_the_crowd_where_that_is_the_finding():
    """"Most postings, fewest applications" is only visible if the bar is the
    postings."""
    shares = [Share("arbeitnow", 12, 523, 12 / 523),
              Share("jooble", 5, 129, 5 / 129)]

    widths = register.bar_widths(shares, "whole")

    assert widths[0] == 1.0
    assert round(widths[1], 3) == round(129 / 523, 3)


def test_a_bar_must_say_what_it_measures():
    """The two panels drawn by one helper compare different things. A meaning
    inferred from the neighbours is a meaning that gets read wrong."""
    with pytest.raises(ValueError):
        register.bar_widths([Share("x", 1, 2, 0.5)], "guess")


def test_a_comparison_with_nothing_in_it_draws_nothing():
    assert register.bar_widths([], "ratio") == []
    assert register.bar_widths([Share("x", 0, 0, 0.0)], "whole") == [0.0]


# --------------------------------------------------------------------------
# What the panel found: claims the data could not carry
# --------------------------------------------------------------------------
def test_a_draft_row_is_not_a_letter():
    """"Anschreiben geschrieben" counted rows, so a draft still being written
    (empty body) and one that failed (never got a body) were both reported as
    letters that exist."""
    steps = {step.key: step for step in register.pipeline(
        _view(drafted=0, drafted_unread=0))}

    assert steps["anschreiben"].count == 0


def test_an_application_with_no_letter_behind_it_says_so():
    """`applied` is not a subset of `drafted` either: pressing "Abgeschickt"
    on a posting whose letter failed records one, and every pre-v10 form
    application was entered that way. The step that is not a subset says so."""
    steps = {step.key: step for step in register.pipeline(
        _view(drafted=2, applied=5, applied_without_letter=3))}

    assert "3 davon ohne Anschreiben" in steps["beworben"].note


def test_a_pipeline_whose_applications_all_had_letters_makes_no_excuse():
    steps = {step.key: step for step in register.pipeline(
        _view(drafted=5, applied=5, applied_without_letter=0))}

    assert steps["beworben"].note == ""


def test_postings_the_scorer_has_not_reached_are_accounted_for():
    """They are in neither figure below "gefunden", so without this the drop
    is partly unexplained and the one note under it reads as the whole
    reason."""
    steps = {step.key: step for step in register.pipeline(
        _view(jobs_total=100, scored_above_zero=60, scored_zero=30))}

    assert "10 noch nicht bewertet" in steps["passend"].note


def test_the_register_never_prints_a_negative_remainder():
    """Two tables counted against each other, and nothing constrains them to
    agree: `total` counts ledger rows and `applied` counts postings that point
    at one."""
    steps = {step.key: step for step in register.ledger(
        _view(applied=9, apps=[_app(row_id=n) for n in range(3)]))}

    assert steps["register"].note == ""


def test_one_application_on_a_third_channel_wins_no_argument():
    """Summed across channels, a single row rendered "1 beantwortet · 100 %",
    was ranked first and produced "Post antwortet häufiger" — a finding
    invented out of one application, on the panel built to refuse exactly
    that."""
    shares = [Share("E-Mail", 9, 35, 9 / 35),
              Share("Online-Portal", 11, 41, 11 / 41),
              Share("Post", 1, 1, 1.0)]

    assert not register.enough_for_a_rate(shares)


def test_every_channel_has_to_carry_the_threshold():
    assert register.enough_for_a_rate(
        [Share("a", 5, 20, 0.25), Share("b", 5, 20, 0.25)])
    assert not register.enough_for_a_rate(
        [Share("a", 5, 20, 0.25), Share("b", 5, 19, 0.26)])
    assert not register.enough_for_a_rate([])


def test_a_bar_can_never_be_drawn_backwards():
    """An application dated in the FUTURE gives a negative age, `width:-98%`
    is invalid CSS that the browser DROPS, and a block element then fills its
    whole column — so the row furthest from overdue drew the longest bar."""
    assert register.clamp(-0.98) == 0.0
    assert register.clamp(1.7) == 1.0
    assert register.clamp(float("nan")) == 0.0
    assert register.clamp(0.42) == 0.42


def test_a_future_dated_application_is_not_drawn_as_the_most_overdue():
    rows = register.silence(
        [_app(gesendet_am="2026-09-30", firma="Zukunft GmbH", row_id=1),
         _app(gesendet_am="2026-07-01", firma="Alt GmbH", row_id=2)],
        14, TODAY)
    longest = max((row.days or 0) for row in rows) or 1

    widths = [register.clamp((row.days or 0) / longest) for row in rows]

    assert max(widths) <= 1.0 and min(widths) >= 0.0
    assert widths[rows.index(next(r for r in rows
                                  if r.firma == "Zukunft GmbH"))] == 0.0


def test_a_letterless_application_is_counted_and_not_inferred(con):
    """`applied` and `drafted` are different SETS. Subtracting them reads as
    zero whenever more letters exist than applications, however many of those
    applications actually carried none."""
    with_letter = _job(con, "with")
    db.upsert_draft(con, with_letter, {"status": "sent",
                                       "anschreiben_body": "Sehr geehrte"})
    db.apply_job(con, with_letter, kanal="E-Mail")
    without = _job(con, "without")
    db.apply_job(con, without, kanal="Online-Portal")
    # …and a third posting that carries a letter but no application, so the
    # aggregate difference (applied 2 - drafted 2) would be zero
    spare = _job(con, "spare")
    db.upsert_draft(con, spare, {"status": "ready",
                                 "anschreiben_body": "Sehr geehrte"})
    con.commit()

    counts = db.pipeline_counts(con)

    assert counts["applied"] == 2
    assert counts["drafted"] == 2
    assert counts["applied_without_letter"] == 1, "measured, not subtracted"
