"""The rail: what it claims, and where each claim comes from.

The numbers are the reason the bar exists, so they are computed as data and
tested as data. Rendering is exercised by the page-rendering tests, which are
the only place a claim can be caught disagreeing with the screen.
"""

import ast
import datetime
import pathlib

import pytest

from jobdeck import db
from jobdeck.services import send
from jobdeck.ui import rail

NOW = datetime.datetime(2026, 8, 12, 18, 0, 0)


def _view(**over) -> dict:
    """A rail's worth of numbers, all quiet unless a test says otherwise."""
    view = {
        "signature": (),
        "profiles": (2, "2026-08-12T17:58:00", 0),
        "unscored": 0,
        "last_scored": "2026-08-12T14:38:00",
        "liveness": ("2026-08-12T09:04:00", 0),
        "jobs_total": 803,
        "companies_total": 640,
        "working": 198,
        "unread": 41,
        "bookmarked": 7,
        "in_progress": 1,
        "apps": [],
        "follow_up_days": 14,
        "sent_today": 2,
        "send_cap": 5,
        "connections": [("Anthropic", True), ("Gmail", True), ("Jooble", True)],
    }
    view.update(over)
    return view


def _rubric(view: dict, key: str, current: str = "stellen") -> rail.Rubric:
    return next(r for r in rail.rubrics(view, current, NOW) if r.key == key)


def _app(status="Gesendet", gesendet_am="2026-08-11"):
    return {"status": status, "gesendet_am": gesendet_am, "firma": "Eine GmbH"}


# ---------------------------------------------------------------------------
# The five rubrics
# ---------------------------------------------------------------------------
def test_the_spine_has_exactly_the_five_rubrics_he_chose():
    assert [r.key for r in rail.rubrics(_view(), "stellen", NOW)] == [
        "unterlagen", "stellen", "bewerbungen", "antworten", "einstellungen"]


def test_stellen_names_both_units_and_compares_like_with_like():
    """Postings and companies are different populations. Printing them as one
    sentence invited the reading that 605 postings had been filtered out, when
    most of the gap is postings collapsing into companies — and the bar was
    companies divided by POSTINGS, so it would have read about a quarter with
    every pile empty."""
    rubric = _rubric(_view(), "stellen")
    assert rubric.count == "41 neu"
    assert rubric.sub == "803 Anzeigen · 198 von 640 Firmen offen"
    assert round(rubric.fill, 3) == round(198 / 640, 3)


def test_the_bar_would_be_full_if_no_pile_hid_anything():
    """The property the old arithmetic could not have: an empty pile set means
    a full bar."""
    rubric = _rubric(_view(working=640, companies_total=640), "stellen")
    assert rubric.fill == 1.0


def test_an_empty_corpus_does_not_divide_by_zero():
    rubric = _rubric(_view(jobs_total=0, companies_total=0, working=0, unread=0),
                     "stellen")
    assert rubric.fill == 0.0
    assert rubric.count == "0 neu"


def test_bewerbungen_leads_with_what_is_still_silent():
    """An application open past the follow-up threshold is the only thing in
    the app that needs him to act on a COMPANY rather than on a posting."""
    view = _view(apps=[_app(gesendet_am="2026-06-10"), _app(gesendet_am="2026-06-11"),
                       _app(status="Absage", gesendet_am="2026-06-12")])
    rubric = _rubric(view, "bewerbungen")
    assert rubric.count == "2 ohne Antwort"
    assert rubric.amber is True
    assert rubric.sub == "3 Bewerbungen · 1 beantwortet"


def test_bewerbungen_stays_quiet_when_nothing_is_overdue():
    view = _view(apps=[_app(gesendet_am="2026-08-11")])
    rubric = _rubric(view, "bewerbungen")
    assert rubric.count == "1 Bewerbungen"
    assert rubric.amber is False


def test_an_application_answered_long_ago_is_not_counted_as_silent():
    """Silence is about waiting, not about age: a rejection from June is
    finished business."""
    view = _view(apps=[_app(status="Absage", gesendet_am="2026-05-01")])
    assert _rubric(view, "bewerbungen").count == "1 Bewerbungen"


def test_unterlagen_reports_a_source_that_stopped_answering():
    rubric = _rubric(_view(profiles=(2, "2026-08-12T17:58:00", 1)), "unterlagen")
    assert rubric.count == "2 Profile"
    assert rubric.sub == "1 Profil ohne Antwort"
    assert rubric.amber is True


def test_one_profile_is_not_called_profile_plural():
    assert _rubric(_view(profiles=(1, "", 0)), "unterlagen").count == "1 Profil"


def test_unterlagen_says_so_when_nothing_has_ever_been_searched():
    rubric = _rubric(_view(profiles=(0, "", 0)), "unterlagen")
    assert rubric.sub == "noch nie gesucht"
    assert rubric.amber is True, "no active profile means nothing arrives at all"


def test_einstellungen_names_the_one_connection_that_is_missing():
    view = _view(connections=[("Anthropic", True), ("Gmail", True),
                              ("Jooble", False)])
    rubric = _rubric(view, "einstellungen")
    assert rubric.count == "2/3"
    assert rubric.sub == "Jooble fehlt"
    assert rubric.amber is True


def test_einstellungen_counts_rather_than_lists_when_several_are_missing():
    view = _view(connections=[("Anthropic", False), ("Gmail", False),
                              ("Jooble", True)])
    assert _rubric(view, "einstellungen").sub == "2 Verbindungen fehlen"


def test_einstellungen_is_quiet_once_everything_is_connected():
    rubric = _rubric(_view(), "einstellungen")
    assert rubric.sub == "alle Verbindungen stehen"
    assert rubric.amber is False


def test_antworten_is_shown_but_cannot_be_opened():
    """Phase 3 is not built. The rubric stands there dimmed so he can see what
    is coming, and it has nowhere to go — a link to an empty screen would be a
    worse answer than a greyed-out name."""
    rubric = _rubric(_view(), "antworten")
    assert rubric.enabled is False
    assert rubric.path == ""


# ---------------------------------------------------------------------------
# The pulse
# ---------------------------------------------------------------------------
def test_a_poll_inside_the_scheduler_window_reads_as_running():
    beat = rail.pulse(_view(profiles=(2, "2026-08-12T17:58:00", 0)), NOW)[0]
    assert (beat.label, beat.state) == ("Suche", "run")
    assert beat.detail == "17:58"


def test_an_older_poll_reads_as_done_rather_than_running():
    beat = rail.pulse(_view(profiles=(2, "2026-08-12T16:00:00", 0)), NOW)[0]
    assert beat.state == "ok"


def test_a_source_never_polled_reads_as_idle():
    beat = rail.pulse(_view(profiles=(0, "", 0)), NOW)[0]
    assert (beat.state, beat.detail) == ("idle", "noch nie")


def test_the_scoring_backlog_is_stated_without_claiming_a_worker():
    """A queue is not evidence that anything is draining it: with AI spend
    switched off — his own default — a backlog of twelve pulsed forever while
    nothing was ever going to score them."""
    beats = rail.pulse(_view(unscored=12, last_scored="2026-08-01T09:00:00"), NOW)
    assert (beats[1].detail, beats[1].state) == ("12 offen", "ok")


def test_a_call_that_just_happened_is_what_makes_the_dot_move():
    beats = rail.pulse(_view(unscored=12, last_scored="2026-08-12T17:58:00"), NOW)
    assert beats[1].state == "run"


def test_scoring_that_has_never_run_reads_as_idle():
    beats = rail.pulse(_view(unscored=12, last_scored=""), NOW)
    assert (beats[1].detail, beats[1].state) == ("12 offen", "idle")


def test_nothing_left_to_score_says_so():
    beats = rail.pulse(_view(unscored=0), NOW)
    assert beats[1].detail == "alles bewertet"


def test_the_liveness_pass_reports_what_it_has_not_reached_yet():
    beats = rail.pulse(_view(liveness=("2026-08-12T17:58:00", 313)), NOW)
    assert (beats[2].detail, beats[2].state) == ("313 offen", "run")


def test_a_finished_liveness_pass_shows_when_it_last_ran():
    beats = rail.pulse(_view(liveness=("2026-08-12T09:04:00", 0)), NOW)
    assert (beats[2].detail, beats[2].state) == ("09:04", "ok")


def test_something_that_happened_on_another_day_shows_its_date():
    """The rail has room for a time or a date, never both — and '09:04' on a
    line that last moved a week ago would be a lie by omission."""
    beats = rail.pulse(_view(liveness=("2026-08-05T09:04:00", 0)), NOW)
    assert beats[2].detail == "05.08."


# ---------------------------------------------------------------------------
# Today's budget
# ---------------------------------------------------------------------------
def test_the_budget_is_what_was_sent_against_the_cap():
    assert rail.budget(_view(sent_today=2, send_cap=5)) == (2, 5)


def test_the_budget_never_reads_as_more_than_the_cap():
    """The cap counts test sends too, and it can be LOWERED while sends are
    already recorded — a bar reading 7 of 5 would be nonsense on screen."""
    assert rail.budget(_view(sent_today=7, send_cap=5)) == (5, 5)


def test_a_negative_cap_is_no_cap_at_all_rather_than_a_crash():
    assert rail.budget(_view(sent_today=3, send_cap=-1)) == (0, 0)


# ---------------------------------------------------------------------------
# Settings are free text he can edit
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["", "   ", "viele", None, "inf", "nan"])
def test_an_unusable_setting_falls_back_instead_of_taking_the_rail_down(raw):
    """The same failure shape that once took the whole inbox down over a
    non-finite age threshold: this is read while every single page is built."""
    assert rail._int_setting(raw, 14) == 14


def test_a_usable_setting_is_honoured():
    assert rail._int_setting("5", 15) == 5
    assert rail._int_setting(" 45 ", 15) == 45


def test_the_rails_send_cap_default_is_the_send_services_own():
    """Two defaults for one number would let the bar promise a budget the send
    path refuses."""
    assert str(rail.SEND_CAP_DEFAULT) == send.DEFAULT_DAILY_CAP


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------
def test_the_facts_come_out_of_the_database(con, data_dir):
    db.add_profile(con, {"name": "Python", "keywords": "python"})
    first = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "a", "title": "Dev",
        "company": "Eine GmbH"})
    db.insert_job_if_new(con, {
        "source": "stub", "external_id": "b", "title": "Dev",
        "company": "Andere GmbH"})
    db.set_job_score(con, first, 80, "passt")
    db.mark_job_opened(con, first)
    db.set_bookmark(con, first, True)
    con.commit()

    view = rail.facts()

    assert view["jobs_total"] == 2
    assert view["working"] == 2, "two companies, nothing hidden"
    assert view["unread"] == 1, "one of them has been opened"
    assert view["bookmarked"] == 1
    assert view["unscored"] == 1
    assert view["profiles"][0] == 1
    assert view["signature"] == rail.signature()


def test_the_working_count_leaves_out_what_the_piles_hide(con, data_dir):
    """The rail must count the same list Stellen shows, or its bar describes a
    screen he cannot get to."""
    keep = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "a", "title": "Dev",
        "company": "Eine GmbH"})
    mismatch = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "b", "title": "Dev",
        "company": "Andere GmbH"})
    db.set_job_score(con, keep, 80, "passt")
    db.set_job_score(con, mismatch, 0, "harte Anforderung verletzt")
    con.commit()

    view = rail.facts()
    assert view["jobs_total"] == 2
    assert view["working"] == 1


def test_a_key_is_reported_as_present_and_never_read(con, data_dir, monkeypatch):
    """A screen that says "connected" must not be holding the value to say it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key")
    names = dict(rail.connections())
    assert names["Anthropic"] is True

    source = pathlib.Path(rail.__file__).read_text()
    assert "anthropic_api_key()" in source
    calls = [ast.unparse(node) for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call)]
    assert all(not call.startswith("ui.label(config.") for call in calls), (
        "a key is being rendered rather than merely counted")
