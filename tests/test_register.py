"""What the Bewerbungen screen states, pinned before anything is drawn.

The two claims worth guarding are the two the real register forced: that the
card's parts always reach the total printed over them, and that the app does
not take credit for the 44 applications that predate it.
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

    # named the way the screen names them, not by the adapter's own key: the
    # panel read "arbeitsagentur · 285" in an otherwise German screen
    assert [s.label for s in shares] == ["Arbeitnow", "Jooble"]
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


def test_a_board_only_gets_credit_for_the_applications_it_carried(con):
    """An imported row has no posting and therefore no board. Folding those
    into a per-source figure would credit a board with work it never did.

    The second jooble posting is what makes `applied` an assertion: with one
    posting that was applied to, `SUM(CASE WHEN bewerbung_id …)` and `COUNT(*)`
    give the same answer, so the whole 'applied' half of the panel was
    unpinned."""
    applied_to = _job(con, "one", source="jooble")
    db.apply_job(con, applied_to, kanal="Online-Portal")
    _job(con, "two", source="jooble")          # delivered, never applied to
    db.add_bewerbung(con, {"firma": "Von Hand GmbH", "kanal": "E-Mail",
                           "status": "Gesendet"})
    con.commit()

    rows = {row["source"]: dict(row) for row in db.applications_by_source(con)}

    assert rows["jooble"]["jobs"] == 2
    assert rows["jooble"]["applied"] == 1, "postings delivered != applied to"
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


def test_only_postings_that_point_at_an_application_are_counted(con):
    """The single population the register block still reads, and the note it
    prints turns on it: "N über JobDeck · M von Hand oder aus der alten
    Liste". Seven other populations were computed beside it for the funnel
    and outlived their only reader by one commit."""
    linked = _job(con, "linked")
    db.apply_job(con, linked, kanal="E-Mail")
    _job(con, "untouched")
    con.commit()

    assert db.count_applied_postings(con) == 1


def test_the_rate_threshold_is_pinned_at_its_own_boundary():
    """Tested at 4 and at 30, the boundary at 20 was never approached — the
    constant could move to 25 with the suite green."""
    at = register.ENOUGH_FOR_A_RATE
    assert register.enough_for_a_rate([Share("a", 1, at, 0.1)])
    assert not register.enough_for_a_rate([Share("a", 1, at - 1, 0.1)])


def test_the_strips_end_labels_are_written_the_way_the_screen_speaks():
    """`de_day` is the only place they are produced, and its body could be
    replaced with `day.isoformat()` — printing "2026-06-18" on a screen that
    is otherwise entirely German — with the suite green."""
    assert register.de_day(datetime.date(2026, 6, 18)) == "18. Juni"
    assert register.de_day(datetime.date(2026, 3, 1)) == "1. März"


def test_a_board_nobody_named_is_still_shown_under_its_own_key():
    """A source added tomorrow must appear, not vanish behind a KeyError or an
    empty label."""
    assert register.source_name("stepstone") == "stepstone"
    assert register.source_name("arbeitsagentur") == "Arbeitsagentur"


def test_a_count_of_one_is_never_printed_with_a_plural():
    """Six German sentences on this screen were written only in the plural, so
    a register of one application read "1 Bewerbungen ohne Antwort", "mit 1
    Bewerbungen" and "1 davon sind über der Schwelle"."""
    assert register.plural(1, "Tag Pause", "Tage Pause") == "1 Tag Pause"
    assert register.plural(36, "Tag Pause", "Tage Pause") == "36 Tage Pause"
    assert register.plural(0, "Tag", "Tage") == "", "zero drops the note"
    assert register.plural(1, "davon wartet", "davon warten", tail=" länger") \
        == "1 davon wartet länger"


# --------------------------------------------------------------------------
# The order the ledger is listed in
# --------------------------------------------------------------------------


def _ordered(apps, sort, follow_up_days=14, today=TODAY):
    """Order a corpus exactly as the screen does: one silence pass, then it."""
    waiting = register.silence(apps, follow_up_days, today)
    return register.order(apps, waiting, sort)


def _ids(rows):
    return [int(row["id"]) for row in rows]


def test_the_default_order_is_the_one_the_query_already_handed_back():
    apps = [_app(row_id=3, gesendet_am="2026-08-10"),
            _app(row_id=2, gesendet_am="2026-08-05"),
            _app(row_id=1, gesendet_am="2026-08-01")]
    assert _ids(_ordered(apps, "date")) == [3, 2, 1]


def test_an_unknown_order_falls_back_to_the_one_the_screen_was_built_around():
    """The rows must differ in every dimension the other orders read, or the
    fallback is indistinguishable from having applied one of them."""
    apps = [_app(row_id=3, firma="Zeta GmbH", gesendet_am="2026-08-15"),
            _app(row_id=2, firma="Alpha GmbH", gesendet_am="2026-06-01"),
            _app(row_id=1, firma="Mitte GmbH", gesendet_am="2026-08-10",
                 status="Absage")]
    assert _ids(_ordered(apps, "nach Lust und Laune")) == [3, 2, 1]
    # ... and is not secretly one of the real orders
    assert _ids(_ordered(apps, "firma")) != [3, 2, 1]
    assert _ids(_ordered(apps, "waiting")) != [3, 2, 1]


def test_a_stored_order_that_names_nothing_we_offer_is_refused():
    assert register.stored_sort("waiting") == "waiting"
    for junk in ("", None, "Wartezeit", "date ", 7, "DATE"):
        # The literal, not `register.DEFAULT_SORT`: comparing the fallback
        # against the constant that defines it is an assertion that moves
        # whenever the thing it guards does, and it passed while the default
        # was mutated to another order.
        assert register.stored_sort(junk) == "date"


def test_the_screen_opens_in_the_order_it_has_always_opened_in():
    assert register.DEFAULT_SORT == "date"
    assert register.DEFAULT_SORT in register.SORT_LABELS


def test_every_offered_order_is_an_order_somebody_implemented():
    """A label with no rule behind it would silently list the default and the
    control would name an order the screen is not in."""
    # Newest-first as `list_bewerbungen` hands them over, and chosen so the
    # three orders cannot coincide: the newest row has waited the least, and
    # the row that is not waiting at all sits between them by date.
    apps = [_app(row_id=1, firma="Zeta GmbH", gesendet_am="2026-08-15"),
            _app(row_id=3, firma="Mitte GmbH", gesendet_am="2026-08-10",
                 status="Absage"),
            _app(row_id=2, firma="Alpha GmbH", gesendet_am="2026-06-01")]
    seen = {key: tuple(_ids(_ordered(apps, key)))
            for key in register.SORT_LABELS}
    assert seen["date"] == (1, 3, 2)
    assert seen["waiting"] == (2, 1, 3)
    assert seen["firma"] == (2, 3, 1)
    assert len(set(seen.values())) == len(register.SORT_LABELS), seen


# -- the differential: the list and the panel above it use ONE waiting rule --


def test_the_waiting_order_is_the_silence_panels_own_order():
    """Not "the same idea as" — the same list. The column counts from the last
    contact and the default order counts from the send date, so a second
    implementation here would drift from the panel it sits under.
    """
    apps = [
        _app(row_id=1, gesendet_am="2026-08-15", firma="Frisch GmbH"),
        _app(row_id=2, gesendet_am="2026-06-01", firma="Alt GmbH"),
        _app(row_id=3, gesendet_am="2026-07-20", firma="Mittel GmbH",
             status="In Bearbeitung"),
        _app(row_id=4, gesendet_am="2026-07-01", firma="Beantwortet GmbH",
             status="Absage"),
    ]
    waiting = register.silence(apps, 14, TODAY)
    listed = register.order(apps, waiting, "waiting")
    assert _ids(listed)[:len(waiting)] == [row.bewerbung_id for row in waiting]


def test_the_list_follows_the_panels_POSITIONS_and_not_its_numbers():
    """The claim in the docstring is that this reads `silence()`'s output
    rather than re-deriving "who has waited longest" from the day counts. The
    two agree on almost every corpus, so only a `waiting` list in an order the
    day counts do NOT explain can tell them apart — and a re-derivation passed
    the whole suite until this existed.
    """
    apps = [_app(row_id=1, status="Gesendet"), _app(row_id=2, status="Gesendet")]
    # Deliberately at odds with the numbers: row 1 has waited longer, and the
    # list must still follow the order it was handed.
    handed = [register.Waiting(bewerbung_id=2, firma="B", days=3, overdue=False),
              register.Waiting(bewerbung_id=1, firma="A", days=99, overdue=True)]
    assert _ids(register.order(apps, handed, "waiting")) == [2, 1]


def test_a_row_sent_today_still_outranks_one_whose_date_cannot_be_read():
    """Zero days is a measurement; no date at all is not. `silence()` files the
    unreadable one last, and reading its day count instead ties them."""
    apps = [_app(row_id=1, gesendet_am="", status="Gesendet"),
            _app(row_id=2, gesendet_am=TODAY.isoformat(), status="Gesendet")]
    assert _ids(_ordered(apps, "waiting")) == [2, 1]


def test_the_waiting_order_holds_over_a_generated_corpus():
    """One rule, many shapes: same-day batches, unreadable dates, every status
    in the vocabulary. The register's real batch is nineteen on one day."""
    from jobdeck.constants import STATUS_OPTIONS
    apps = []
    for index in range(60):
        apps.append(_app(
            row_id=index + 1,
            status=STATUS_OPTIONS[index % len(STATUS_OPTIONS)],
            # Deliberate collisions: a batch sent on one day has no order of
            # its own, and two of them state no date at all.
            gesendet_am="" if index in (11, 41)
            else f"2026-0{6 + index % 3}-{1 + index % 20:02d}",
            firma=f"Firma {index % 7}",
        ))
    waiting = register.silence(apps, 14, TODAY)
    listed = register.order(apps, waiting, "waiting")
    assert _ids(listed)[:len(waiting)] == [row.bewerbung_id for row in waiting]
    assert sorted(_ids(listed)) == sorted(_ids(apps))


def test_an_application_that_is_not_waiting_cannot_lead_an_order_about_waiting():
    apps = [_app(row_id=1, gesendet_am="2026-01-01", status="Absage"),
            _app(row_id=2, gesendet_am="2026-08-14", status="Gesendet")]
    assert _ids(_ordered(apps, "waiting")) == [2, 1]


def test_the_rows_that_are_not_waiting_keep_the_order_they_arrived_in():
    apps = [_app(row_id=9, gesendet_am="2026-08-12", status="Absage"),
            _app(row_id=8, gesendet_am="2026-08-11", status="Keine Antwort"),
            _app(row_id=7, gesendet_am="2026-08-10", status="Zurückgezogen"),
            _app(row_id=1, gesendet_am="2026-05-01", status="Gesendet")]
    assert _ids(_ordered(apps, "waiting")) == [1, 9, 8, 7]


def test_a_row_whose_age_cannot_be_read_claims_no_place_at_the_top():
    """`silence()` files an unreadable date last among the waiting; the list
    must agree with it rather than sort the row to wherever its id fell."""
    apps = [_app(row_id=1, gesendet_am="", status="Gesendet"),
            _app(row_id=2, gesendet_am="2026-08-14", status="Gesendet"),
            _app(row_id=3, gesendet_am="2026-06-01", status="Gesendet")]
    assert _ids(_ordered(apps, "waiting")) == [3, 2, 1]


# -- alphabetical --


def test_a_lowercase_name_sorts_by_its_letter_and_not_by_its_code_point():
    """Twenty companies in the real ledger are spelled in lower case. Raw code
    points put every one of them after every capitalised name, so the
    discriminating pair is a lower-case name that must come FIRST.
    """
    apps = [_app(row_id=1, firma="Beta GmbH"),
            _app(row_id=2, firma="alpha GmbH")]
    assert _ids(_ordered(apps, "firma")) == [2, 1]


def test_an_umlaut_sorts_where_a_reader_looks_for_it():
    apps = [_app(row_id=1, firma="Zeta GmbH"),
            _app(row_id=2, firma="Übersicht GmbH"),
            _app(row_id=3, firma="Alpha GmbH")]
    # Under 'U', not after "Zeta" — which is where a raw code-point sort files
    # every umlaut, past 'z'.
    assert _ids(_ordered(apps, "firma")) == [3, 2, 1]


def test_an_umlaut_ranks_among_its_base_letter_and_not_after_it():
    """NFKD alone moves the base letter to the front, which is enough to keep
    an umlaut out of the far end of the list — so only a tie ON that base
    letter separates decomposing from actually dropping the mark. DIN 5007-1
    says 'u' with a diaeresis IS 'u', so it sorts before "Uz", not after it.
    Left in, the combining mark is code point 776 and outranks every letter.
    """
    apps = [_app(row_id=1, firma="Uz GmbH"),
            _app(row_id=2, firma="Über GmbH")]
    assert _ids(_ordered(apps, "firma")) == [2, 1]


def test_the_sharp_s_sorts_as_the_two_letters_it_stands_for():
    """DIN 5007-1, and the one part `casefold` already does.

    The pair has to diverge AT the ß and nowhere earlier. "Strassburg" against
    "Strauß" does not: they part at 's' vs 'u', four characters before it, so
    the folding half of the claim went untested. Here both keys agree up to
    "strass"; only expanding the ß decides, and it decides the other way round
    from the raw code point (U+00DF is past every letter).
    """
    # No legal form on either name: a differing suffix would decide the
    # comparison after the ß and hide exactly what this pins.
    apps = [_app(row_id=1, firma="Straßen"), _app(row_id=2, firma="Strassen")]
    # folded both are "strassen", so the tie falls back to the incoming order.
    # Unfolded, 'ß' (U+00DF) is past 's' and row 1 would go last.
    assert _ids(_ordered(apps, "firma")) == [1, 2]

    apps = [_app(row_id=1, firma="Straße"), _app(row_id=2, firma="Strassen")]
    # folded: "strasse" < "strassen".  unfolded: "strassen" < "straße".
    assert _ids(_ordered(apps, "firma")) == [1, 2]


def test_a_stroke_or_a_ligature_files_under_its_base_letter():
    """NFKD decomposes an accent because a combining mark is a separate code
    point; a STROKE is part of the letter and a LIGATURE is one letter, so
    neither decomposes. Ø, Ł, Æ, Œ and Đ therefore filed after every name
    beginning "z" — the exact failure the key exists to prevent, surviving the
    first fix for the letters it could not reach.
    """
    names = ["Zeta GmbH", "Ørsted", "Łukasiewicz", "Æther AG", "Đuro",
             "Œuvre", "Abend GmbH"]
    apps = [_app(row_id=i, firma=n) for i, n in enumerate(names, start=1)]

    listed = [str(row["firma"]) for row in _ordered(apps, "firma")]

    assert listed == ["Abend GmbH", "Æther AG", "Đuro", "Łukasiewicz",
                      "Œuvre", "Ørsted", "Zeta GmbH"]


def test_two_rows_of_one_name_keep_the_newest_first():
    apps = [_app(row_id=2, firma="Gleiche GmbH", gesendet_am="2026-08-10"),
            _app(row_id=1, firma="Gleiche GmbH", gesendet_am="2026-08-01")]
    assert _ids(_ordered(apps, "firma")) == [2, 1]


def test_a_row_with_no_company_name_leads_rather_than_disappearing():
    """`fold(None)` is the empty string, which sorts before every name — so a
    nameless row goes to the TOP, where it is at least visible. The earlier
    assertion sorted the ids before comparing, so it proved only that nothing
    raised and nothing was lost, and would have passed wherever the row landed.
    """
    apps = [_app(row_id=1, firma="Alpha GmbH"), _app(row_id=2, firma=None),
            _app(row_id=3, firma="")]
    assert _ids(_ordered(apps, "firma")) == [2, 3, 1]


def test_no_order_ever_loses_or_invents_a_row():
    apps = [_app(row_id=index, status=("Gesendet", "Absage")[index % 2],
                 firma=f"Firma {index}") for index in range(1, 12)]
    for key in register.SORT_LABELS:
        assert sorted(_ids(_ordered(apps, key))) == list(range(1, 12))


# -- when the order has nothing to separate --


def test_an_order_about_waiting_says_so_where_nothing_waits():
    """"Absage", "Keine Antwort" and "Antwort erhalten" hold no application
    that is still waiting, and beside the control sits the filter that shows
    exactly those — 56 rows of his 141, three of the six views."""
    rows = [_app(row_id=1, status="Absage"), _app(row_id=2, status="Keine Antwort")]
    waiting = register.silence(rows, 14, TODAY)

    assert register.order_note(rows, waiting, "waiting")
    assert "wartet noch" in register.order_note(rows, waiting, "waiting")


def test_one_waiting_row_is_enough_for_the_order_to_mean_something():
    rows = [_app(row_id=1, status="Absage"), _app(row_id=2, status="Gesendet")]
    waiting = register.silence(rows, 14, TODAY)

    assert register.order_note(rows, waiting, "waiting") == ""


def test_the_note_is_about_the_rows_on_screen_and_not_the_whole_ledger():
    """The filter is what empties the column, so the question has to be asked
    of what is listed — asking the register would keep the note off the very
    views it exists for."""
    listed = [_app(row_id=1, status="Absage")]
    # The ledger still holds a waiting application; it is filtered out.
    waiting = register.silence([_app(row_id=2, status="Gesendet")], 14, TODAY)

    assert register.order_note(listed, waiting, "waiting")


def test_no_other_order_claims_anything_about_waiting():
    rows = [_app(row_id=1, status="Absage")]
    waiting = register.silence(rows, 14, TODAY)
    for key in register.SORT_LABELS:
        if key != "waiting":
            assert register.order_note(rows, waiting, key) == ""


def test_an_empty_list_is_already_answered_by_the_line_under_it():
    waiting = register.silence([], 14, TODAY)
    assert register.order_note([], waiting, "waiting") == ""
