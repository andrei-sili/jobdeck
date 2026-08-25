"""The rail: what it claims, and where each claim comes from.

The numbers are the reason the bar exists, so they are computed as data and
tested as data. Rendering is exercised by the page-rendering tests, which are
the only place a claim can be caught disagreeing with the screen.
"""

import ast
import datetime
import pathlib

import pytest

from jobdeck import constants, db
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
        "started": 0,
        "apps": [],
        "follow_up_days": 14,
        "sent_today": 2,
        "send_cap": 5,
        "connections": [("Anthropic", True), ("Gmail", True), ("Jooble", True)],
        "replies_pending": 0,
        "replies_total": 0,
        "replies_recent_einladung": 0,
        "replies_last_poll": "2026-08-12T17:55:00",
        "replies_last_error": "",
        "gmail_can_read": True,
        "unterlagen": {"template_ok": True, "anlagen": 6,
                       "folder_state": "ok", "built": True, "documents": 7},
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
    # "überfällig", not "ohne Antwort": the screen one click away uses those
    # three words for EVERY open application, and this counts only the ones
    # past the threshold — two different numbers under one wording.
    assert rubric.count == "2 überfällig"
    assert rubric.amber is True
    assert rubric.sub == "3 Bewerbungen · 1 beantwortet"


def test_bewerbungen_stays_quiet_when_nothing_is_overdue():
    view = _view(apps=[_app(gesendet_am="2026-08-11")])
    rubric = _rubric(view, "bewerbungen")
    assert rubric.count == "1 Bewerbungen"
    assert rubric.amber is False


def test_the_overdue_count_reads_the_clock_it_was_given():
    """`rubrics` is documented as pure, and the overdue arm was not: it asked
    `date.today()`, so the one number in the rail that moves by itself was the
    one number no test could hold still. The test above pinned a send date 1
    day before NOW and went red fourteen days later, on a day nobody had
    touched the code.

    Both directions are asserted from ONE view: the same input, two clocks,
    two different answers — which is the property, and which no wall-clock
    reading can satisfy."""
    view = _view(apps=[_app(gesendet_am="2026-08-11")])
    day_after = _rubric(view, "bewerbungen")                      # NOW: 08-12
    fortnight = next(
        r for r in rail.rubrics(view, "stellen",
                                datetime.datetime(2026, 8, 25, 18, 0, 0))
        if r.key == "bewerbungen")
    assert (day_after.count, day_after.amber) == ("1 Bewerbungen", False)
    assert (fortnight.count, fortnight.amber) == ("1 überfällig", True)


def test_an_application_answered_long_ago_is_not_counted_as_silent():
    """Silence is about waiting, not about age: a rejection from June is
    finished business."""
    view = _view(apps=[_app(status="Absage", gesendet_am="2026-05-01")])
    assert _rubric(view, "bewerbungen").count == "1 Bewerbungen"


def _docs(**over) -> dict:
    facts = {"template_ok": True, "anlagen": 6, "folder_state": "ok",
             "built": True, "documents": 7}
    facts.update(over)
    return facts


def test_unterlagen_counts_documents_and_not_search_profiles():
    """It used to read "3 Profile" under the heading Unterlagen — a true
    number about something else entirely, on the one rubric he opened looking
    for his CV."""
    rubric = _rubric(_view(profiles=(3, "2026-08-12T17:58:00", 0)), "unterlagen")
    assert rubric.count == "7 Dokumente"
    assert rubric.sub == "Vorlage + 6 Anlagen"
    assert rubric.amber is False
    assert rubric.fill == 1.0


def test_one_document_is_not_called_dokumente_plural():
    rubric = _rubric(_view(unterlagen=_docs(anlagen=0, documents=1)),
                     "unterlagen")
    assert rubric.count == "1 Dokument"


def test_one_anlage_is_not_called_anlagen_plural():
    rubric = _rubric(_view(unterlagen=_docs(anlagen=1, documents=2)),
                     "unterlagen")
    assert rubric.sub == "Vorlage + 1 Anlage"


@pytest.mark.parametrize("facts,expected", [
    ({"template_ok": False}, "Vorlage fehlt"),
    ({"folder_state": "unset", "anlagen": 0, "documents": 1},
     "kein Ordner für Anlagen"),
    ({"folder_state": "missing", "anlagen": 0, "documents": 1},
     "Anlagen-Ordner fehlt"),
    ({"folder_state": "empty", "anlagen": 0, "documents": 1},
     "keine Anlagen — nur der Brief"),
    ({"built": False}, "Mappe noch nie gebaut"),
])
def test_the_rubric_names_the_first_thing_standing_in_the_way(facts, expected):
    """In the order they block each other: without the template there is no
    letter to attach anything to, without an Anlage the Mappe is the letter
    alone, and without a build nothing on the screen has been measured."""
    rubric = _rubric(_view(unterlagen=_docs(**facts)), "unterlagen")
    assert rubric.sub == expected
    assert rubric.amber is True


def test_the_bar_fills_as_the_mappe_becomes_sendable():
    """Three parts, and an employer needs all three."""
    nothing = _rubric(_view(unterlagen=_docs(
        template_ok=False, anlagen=0, folder_state="unset", built=False,
        documents=0)), "unterlagen")
    half = _rubric(_view(unterlagen=_docs(built=False)), "unterlagen")
    whole = _rubric(_view(), "unterlagen")
    assert (nothing.fill, round(half.fill, 3), whole.fill) == (0.0, 0.667, 1.0)


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


def test_antworten_opens_and_reports_the_quiet_state():
    rubric = _rubric(_view(), "antworten")
    assert rubric.enabled is True
    assert rubric.path == "/antworten"
    assert rubric.count == "0 Antworten"
    assert rubric.sub == "Gmail liest mit · zuletzt 17:55"
    assert rubric.amber is False


def test_antworten_counts_the_review_pile_first():
    rubric = _rubric(_view(replies_pending=3, replies_total=8), "antworten")
    assert rubric.count == "3 zu prüfen"
    assert rubric.amber is True


def test_a_fresh_invitation_outranks_everything():
    rubric = _rubric(_view(replies_pending=3, replies_total=8,
                           replies_recent_einladung=1), "antworten")
    assert rubric.count == "1 Einladung!"
    assert rubric.amber is True


def test_one_settled_reply_is_not_called_antworten_plural():
    rubric = _rubric(_view(replies_total=1), "antworten")
    assert rubric.count == "1 Antwort"


def test_antworten_names_what_a_reconnect_would_add():
    """A pre-Phase-3 token sends but cannot read; the rubric must say that
    instead of pretending the inbox is empty."""
    rubric = _rubric(_view(gmail_can_read=False), "antworten")
    assert rubric.sub == "Gmail ohne Lese-Zugriff — neu verbinden"
    assert rubric.amber is True


def test_antworten_reports_a_disconnected_gmail_before_the_scope():
    rubric = _rubric(_view(
        connections=[("Anthropic", True), ("Gmail", False), ("Jooble", True)],
        gmail_can_read=False), "antworten")
    assert rubric.sub == "Gmail ist nicht verbunden"


def test_a_failing_reader_is_reported_not_hidden():
    rubric = _rubric(_view(replies_last_error="boom"), "antworten")
    assert rubric.sub == "Gmail-Lesen gestört"
    assert rubric.amber is True


def test_antworten_before_the_first_pass_says_so():
    rubric = _rubric(_view(replies_last_poll=""), "antworten")
    assert rubric.sub == "Gmail liest mit — erster Lauf steht aus"


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
    beat = rail.pulse(_view(profiles=(1, "", 0)), NOW)[0]
    assert (beat.state, beat.detail) == ("idle", "noch nie")


def test_the_puls_carries_what_the_documents_rubric_no_longer_says():
    """A search profile is not a document. The two facts that used to sit
    under "Unterlagen" belong on the line that reports the engine."""
    beat = rail.pulse(_view(profiles=(2, "2026-08-12T17:58:00", 1)), NOW)[0]
    assert beat.detail == "1 Profil ohne Antwort"


def test_a_refusing_source_outranks_the_clock():
    """The clock would say the pass ran — true, and beside the point when it
    came back with nothing."""
    beats = rail.pulse(_view(profiles=(3, "2026-08-12T17:58:00", 2)), NOW)
    assert beats[0].detail == "2 Profile ohne Antwort"


def test_no_active_profile_at_all_is_stated_rather_than_read_as_quiet():
    """Nothing will ever arrive, and "zuletzt gesucht 17:58" would look
    exactly like a healthy app."""
    beat = rail.pulse(_view(profiles=(0, "2026-08-12T17:58:00", 0)), NOW)[0]
    assert (beat.state, beat.detail) == ("warn", "kein aktives Profil")


def test_a_source_that_stopped_answering_keeps_its_colour():
    """The amber used to hang on the Unterlagen rubric. Moving the profiles to
    the Puls must not drop it — a board that has stopped answering would then
    read exactly like a healthy one, and discovery is the top of the funnel."""
    quiet = rail.pulse(_view(profiles=(2, "2026-08-12T14:38:00", 0)), NOW)[0]
    broken = rail.pulse(_view(profiles=(2, "2026-08-12T14:38:00", 1)), NOW)[0]

    assert quiet.state == "ok"          # ran a while ago, answered fine
    assert broken.state == "warn"
    assert broken.detail == "1 Profil ohne Antwort"


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


def test_bounded_settings_match_their_workflow_defaults():
    assert rail._int_setting("-1", 14, minimum=1, clamp=False) == 14
    assert rail._int_setting("-1", 15, minimum=0) == 0


def test_the_rails_send_cap_default_is_the_send_services_own():
    """Two defaults for one number would let the bar promise a budget the send
    path refuses."""
    assert str(rail.SEND_CAP_DEFAULT) == constants.DEFAULT_DAILY_CAP


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
    # deliberately NOT shaped like a real key: a public repo runs
    # secret scanners, and a convincing placeholder is a false alarm
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-for-this-test")
    names = dict(rail.connections())
    assert names["Anthropic"] is True

    source = pathlib.Path(rail.__file__).read_text()
    assert "anthropic_api_key()" in source
    calls = [ast.unparse(node) for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call)]
    assert all(not call.startswith("ui.label(config.") for call in calls), (
        "a key is being rendered rather than merely counted")


def test_a_running_form_application_is_visible_from_every_screen():
    """It is an application that may already be out, and the only state the
    app cannot resolve by itself — so it is worth seeing from Einstellungen."""
    rubric = _rubric(_view(started=3), "stellen")
    assert "3 laufen" in rubric.sub
    assert rubric.amber is True


def test_nothing_running_says_nothing():
    """A permanent "0 laufen" is a line you stop reading, and then it is not
    there when it matters."""
    rubric = _rubric(_view(started=0), "stellen")
    assert "laufen" not in rubric.sub
    assert rubric.amber is False


def test_the_rail_counts_the_running_forms_from_the_database(con, data_dir):
    """`facts()["started"]` could be hardcoded to 0 with the suite green: the
    rubric tests all feed a hand-written view."""
    from jobdeck import db
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "e1", "title": "Entwickler",
        "company": "Firma", "url": "https://firma.de/x"})
    db.mark_form_opened(con, job_id)
    con.commit()

    assert rail.facts()["started"] == 1


# --------------------------------------------------------------------------
# The Postausgang shelf: present only while something is in it
# --------------------------------------------------------------------------
def test_the_shelf_states_what_waits_and_what_may_still_leave_today():
    """It carries the one figure that decides whether pressing Senden is even
    possible, which until now he only met inside the send screen itself."""
    assert rail.shelf(_view(in_progress=2, sent_today=2, send_cap=5)) == (
        "2 Briefe warten · 3 von 5 heute frei")


def test_one_waiting_letter_is_not_two():
    assert rail.shelf(_view(in_progress=1, sent_today=0, send_cap=5)) \
        .startswith("1 Brief wartet ·")


def test_an_empty_queue_gets_no_shelf_at_all():
    """His decision: a rubric found empty nine times out of ten teaches you to
    ignore it, so it disappears rather than reading "0 warten"."""
    assert rail.shelf(_view(in_progress=0)) == ""


def test_a_used_up_budget_never_reads_as_a_negative_allowance():
    """The cap counts test sends too, so it can be spent past its own figure —
    and "-2 von 5 heute frei" beside a stack of letters is nonsense."""
    assert "0 von 5 heute frei" in rail.shelf(
        _view(in_progress=3, sent_today=9, send_cap=5))


def test_the_shelf_counts_what_the_tab_behind_it_actually_holds():
    """`count_waiting_drafts` is the "prepare N a day" quota and counts only
    letters that could be SENT. Used for the shelf, a register holding one
    failed draft and one stuck send drew no shelf at all while the tab behind
    it held two rows each waiting on a decision — and the shelf is that tab's
    only entry point."""
    from jobdeck import db as db_module
    assert set(db_module.OPEN_DRAFT_STATUSES) == {
        "generating", "ready", "failed", "approved", "sending"}


def test_a_failed_letter_still_raises_the_shelf():
    assert rail.shelf(_view(in_progress=1, sent_today=0, send_cap=5))
