"""The four things he asked for, on the screen he asked for them on.

"unde este butonul sau cum se trigereaza cautarea joburilor potrivite … cum se
actualizeaza lista? cum dispar cele vechi … care eu apas ca nam interes?"

The search button existed — in Einstellungen, in English. The score filter did
not exist at all. Sorting by date did not exist. And "not interested" reached
one advert, so he pressed it three times on one staffing agency.
"""

import asyncio
import datetime
import sys

import pytest
from nicegui import ui
from nicegui.testing import User

from jobdeck import db
from jobdeck.services import polling
from jobdeck.ui.pages import jobs

pytest_plugins = ["nicegui.testing.user_plugin"]

pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_main.py")


@pytest.fixture(autouse=True)
def _keep_the_package_importable():
    saved = {name: mod for name, mod in sys.modules.items()
             if name == "jobdeck" or name.startswith("jobdeck.")}
    yield
    sys.modules.update(saved)


def _job(con, ext, company, score, days_old=1):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": ext, "title": f"Entwickler {ext}",
        "company": company, "url": f"https://example.invalid/{ext}",
    })
    day = (datetime.date.today() - datetime.timedelta(days=days_old)).isoformat()
    con.execute("UPDATE jobs SET published_on=? WHERE id=?", (day, job_id))
    db.set_job_score(con, job_id, score, "weil")
    con.commit()
    return job_id


def _marked(user: User, marker: str) -> list:
    with user.client:
        return [el for el in user.client.elements.values()
                if marker in getattr(el, "_markers", [])]


def _row_companies(user: User) -> list[str]:
    """The company names in the LIST — not in the reading pane, which keeps
    showing the posting under his cursor on purpose after it leaves the view."""
    with user.client:
        rows = [el for el in user.client.elements.values()
                if "jd-row" in getattr(el, "_classes", [])]
        names = []
        for row in rows:
            labels = [d for d in row.descendants()
                      if isinstance(d, ui.label) and d.text]
            if labels:
                names.append(labels[0].text)
        return names


# --------------------------------------------------------- what the line says


@pytest.mark.parametrize("report,expected", [
    ({}, "Noch nie gesucht."),
    ({"at": "2026-08-20T13:02:00", "by": "schedule", "new": 12,
      "known": 231, "duplicate": 4},
     "Automatisch gesucht 13:02 — 12 neue Anzeigen · 231 schon bekannt · "
     "4 bei Firmen, bei denen du dich schon beworben hast"),
    ({"at": "2026-08-20T19:40:00", "by": "user", "new": 1, "known": 0,
      "duplicate": 0},
     "Von dir gestartet 19:40 — 1 neue Anzeige"),
    ({"at": "2026-08-20T19:40:00", "by": "user", "new": 0, "known": 8,
      "duplicate": 0},
     "Von dir gestartet 19:40 — 0 neue Anzeigen · 8 schon bekannt"),
])
def test_the_line_says_who_searched_and_what_came_back(report, expected):
    """The first word answers "cind?". A search that found nothing used to look
    exactly like a search that never ran — and "0 neue Anzeigen" is a result
    while "never searched" is not, so they are different sentences."""
    assert jobs.poll_line(report) == expected


def test_one_new_posting_is_not_called_anzeigen_plural():
    assert "1 neue Anzeige —" not in jobs.poll_line(
        {"at": "2026-08-20T10:00:00", "by": "user", "new": 1})
    assert jobs.poll_line({"at": "2026-08-20T10:00:00", "by": "user",
                           "new": 1}).endswith("1 neue Anzeige")


# ------------------------------------------------------------- on the screen


async def test_the_search_button_is_on_the_list_screen(user: User, con, data_dir):
    """It existed — as "Poll all profiles now", in Einstellungen, in English.
    He looked for it here."""
    _job(con, "a", "Firma GmbH", 80)

    await user.open("/")

    assert _marked(user, "poll-now"), "no search button on Stellen"
    await user.should_see("Jetzt suchen")
    await user.should_see("Noch nie gesucht.")


async def test_the_search_button_left_einstellungen(user: User, con, data_dir):
    """Moved, not duplicated: two buttons for one pass is two places for the
    single-flight rule to be forgotten."""
    await user.open("/settings")

    await user.should_not_see("Poll all profiles now")


async def test_pressing_it_really_searches_and_says_what_it_found(
        user: User, con, data_dir, monkeypatch):
    """Driven through the control, because `poll_all_profiles` is a COROUTINE:
    handing it to run.io_bound would give a worker thread an un-awaited
    coroutine, and nothing on screen would say so."""
    db.add_profile(con, {
        "name": "Python", "keywords": "python", "location": "", "radius_km": 0,
        "remote": 0, "sources": '["stub"]', "active": 1, "interval_minutes": 60,
    })
    con.commit()

    async def found(profile):
        return {"new": 2, "duplicate": 1, "known": 7}

    monkeypatch.setattr(polling, "poll_profile", found)
    await user.open("/")
    user.find(marker="poll-now").click()
    await user.should_see("Von dir gestartet")

    await user.should_see("2 neue Anzeigen")
    await user.should_see("1 bei Firmen, bei denen du dich schon beworben hast")


async def test_a_failing_search_says_so_instead_of_dying_quietly(
        user: User, con, data_dir, monkeypatch, caplog):
    """An exception out of a NiceGUI handler is one line in the log and a
    button that looks alive."""
    db.add_profile(con, {
        "name": "Python", "keywords": "python", "location": "", "radius_km": 0,
        "remote": 0, "sources": '["stub"]', "active": 1, "interval_minutes": 60,
    })
    con.commit()

    async def refuses(profile):
        raise RuntimeError("die Quelle antwortet nicht")

    monkeypatch.setattr(polling, "poll_profile", refuses)
    await user.open("/")
    user.find(marker="poll-now").click()

    await user.should_see("Die Suche ist gescheitert")
    # …and the button is usable again
    assert _marked(user, "poll-now")[0].enabled
    # the traceback belongs in the log — it is expected here, and the suite
    # otherwise fails a test that produced one
    logged = [r for r in caplog.get_records("call") if r.levelname == "ERROR"]
    assert [r.message for r in logged] == ["manual poll failed"]
    caplog.get_records("call").clear()


# ------------------------------------------------------------- the two filters


async def test_the_list_opens_with_the_floor_already_down(user: User, con,
                                                          data_dir):
    """The default is 40 because that is where the measurement puts the cut:
    300 postings, 222 of them under 40."""
    strong = _job(con, "a", "Alpha GmbH", 80)
    _job(con, "b", "Beta GmbH", 20)

    await user.open("/")

    assert jobs.DEFAULT_MIN_SCORE == 40
    assert _row_companies(user) == ["Alpha GmbH"]
    assert strong


async def test_the_floor_can_be_taken_off_again(user: User, con, data_dir):
    """Nothing is deleted — it is a named, reversible view of the same rows."""
    _job(con, "a", "Alpha GmbH", 80)
    _job(con, "b", "Beta GmbH", 20)
    await user.open("/")
    await user.should_not_see("Beta GmbH")

    _marked(user, "score-select")[0].set_value(0)
    await asyncio.sleep(0.2)

    await user.should_see("Beta GmbH")


async def test_the_floor_never_empties_the_pile_that_exists_to_be_looked_at(
        user: User, con, data_dir):
    """"Passt nicht" IS the score-0 pile. A floor applied there would empty the
    very view he opened to see what was set aside."""
    _job(con, "a", "Alpha GmbH", 0)
    await user.open("/")

    _marked(user, "view-select")[0].set_value("passt_nicht")
    await asyncio.sleep(0.2)

    await user.should_see("Alpha GmbH")


async def test_the_list_opens_newest_first(user: User, con, data_dir):
    """His words: filters on score, DEFAULT on publication date."""
    _job(con, "a", "Alt GmbH", 90, days_old=20)
    _job(con, "b", "Neu GmbH", 60, days_old=1)

    await user.open("/")

    with user.client:
        labels = [el.text for el in user.client.elements.values()
                  if isinstance(el, ui.label) and el.text in
                  ("Alt GmbH", "Neu GmbH")]
    assert labels[0] == "Neu GmbH", "the list did not open newest first"


async def test_he_can_ask_for_the_best_match_first_instead(user: User, con,
                                                           data_dir):
    _job(con, "a", "Alt GmbH", 90, days_old=20)
    _job(con, "b", "Neu GmbH", 60, days_old=1)
    await user.open("/")

    _marked(user, "sort-select")[0].set_value("score")
    await asyncio.sleep(0.2)

    with user.client:
        labels = [el.text for el in user.client.elements.values()
                  if isinstance(el, ui.label) and el.text in
                  ("Alt GmbH", "Neu GmbH")]
    assert labels[0] == "Alt GmbH"


# ------------------------------------------------------ x reaches the company


async def test_x_hides_the_whole_company(user: User, con, data_dir):
    """The complaint, exactly: he pressed it three times on one staffing
    agency, because each press reached one advert."""
    # distinct dates, so which row the reader opens on is decided by the sort
    # rather than by a tie — the list opens newest first
    for n in range(3):
        _job(con, f"z{n}", "Zeitarbeit GmbH", 80, days_old=1)
    _job(con, "k", "Andere GmbH", 80, days_old=5)

    await user.open("/")
    await user.should_see("Zeitarbeit GmbH")
    user.find(marker="job-x").click()
    await user.should_see("ausgeblendet")

    assert _row_companies(user) == ["Andere GmbH"], \
        "all three adverts of the agency should have gone, not just one"
    with db.db() as fresh:
        assert [r["company"] for r in db.list_hidden_companies(fresh)] == \
            ["Zeitarbeit GmbH"]


async def test_the_undo_bar_names_the_price(user: User, con, data_dir):
    """"26 Anzeigen, die beste mit Bewertung 74" is a decision;
    "ausgeblendet" is a guess. And it has to say that it keeps applying —
    that is the whole difference from putting one advert away."""
    _job(con, "a", "Zeitarbeit GmbH", 74)
    _job(con, "b", "Zeitarbeit GmbH", 60)

    await user.open("/")
    user.find(marker="job-x").click()

    await user.should_see("2 Anzeigen")
    await user.should_see("die beste mit Bewertung 74")
    await user.should_see("Gilt auch für neue Suchen")


async def test_the_undo_really_brings_the_company_back(user: User, con,
                                                       data_dir):
    _job(con, "a", "Zeitarbeit GmbH", 80)
    await user.open("/")
    user.find(marker="job-x").click()
    await user.should_see("ausgeblendet")
    with db.db() as fresh:
        assert db.count_hidden_companies(fresh) == 1

    user.find(marker="undo-hide").click()
    await user.should_see("wieder in der Liste")

    assert _row_companies(user) == ["Zeitarbeit GmbH"]
    with db.db() as fresh:
        assert db.count_hidden_companies(fresh) == 0


async def test_hiding_reaches_an_advert_that_arrives_later(user: User, con,
                                                           data_dir):
    """The reason it is a company and not a status: the next search must not
    bring it straight back."""
    _job(con, "a", "Zeitarbeit GmbH", 80)
    await user.open("/")
    user.find(marker="job-x").click()
    await user.should_see("ausgeblendet")

    _job(con, "b", "Zeitarbeit GmbH", 90, days_old=0)
    with db.db() as fresh:
        visible = db.list_jobs(fresh, status="new", hidden="exclude")
    assert visible == []


async def test_a_posting_with_no_company_is_still_put_away_on_its_own(
        user: User, con, data_dir):
    """A blank field is missing information, not an employer — there is
    nothing to hide but this row, and the old behaviour is right."""
    job_id = _job(con, "a", "", 80)

    await user.open("/")
    user.find(marker="job-x").click()
    await asyncio.sleep(0.2)

    with db.db() as fresh:
        assert db.get_job(fresh, job_id)["status"] == "skipped"
        assert db.count_hidden_companies(fresh) == 0


async def test_the_hidden_companies_have_a_view_of_their_own(user: User, con,
                                                             data_dir):
    """Nothing is deleted, only moved into a view with a name."""
    _job(con, "a", "Zeitarbeit GmbH", 80)
    await user.open("/")
    user.find(marker="job-x").click()
    await user.should_see("ausgeblendet")

    _marked(user, "view-select")[0].set_value("ausgeblendet")
    await asyncio.sleep(0.3)

    await user.should_see("Zeitarbeit GmbH")
    assert _marked(user, "unhide-company"), "no way back from the pile"


async def test_the_way_back_from_the_pile_works(user: User, con, data_dir):
    _job(con, "a", "Zeitarbeit GmbH", 80)
    await user.open("/")
    user.find(marker="job-x").click()
    await user.should_see("ausgeblendet")
    _marked(user, "view-select")[0].set_value("ausgeblendet")
    await asyncio.sleep(0.3)

    user.find(marker="unhide-company").click()
    await user.should_see("wieder in der Liste")

    with db.db() as fresh:
        assert db.count_hidden_companies(fresh) == 0


# ------------------------------------------------- less on screen, not more


async def test_the_view_control_no_longer_lists_eleven_things(user: User, con,
                                                              data_dir):
    """He rejected the arrangement twice for having too much on it. The piles
    are still views — nothing is deleted — but a pile is found by its NUMBER
    under the list, not by scrolling a dropdown looking for a word."""
    _job(con, "a", "Firma GmbH", 80)

    await user.open("/")

    offered = _marked(user, "view-select")[0].options
    assert len(offered) == len(jobs.MAIN_VIEW_KEYS) < len(jobs.VIEWS)
    assert "passt_nicht" not in offered
    assert "ausgeblendet" not in offered


async def test_every_posting_is_still_reachable_from_the_screen(user: User, con,
                                                                data_dir):
    """The rule this change could quietly break: nothing is ever deleted, so
    every posting has to be findable — and a view that is neither in the
    control nor behind a number is a view he cannot open."""
    reachable = set(jobs.MAIN_VIEW_KEYS)
    for view in jobs.VIEWS:
        parts = jobs.hidden_parts(
            view, {"mismatches": 1, "dead": 1, "applied_firm": 1, "old": 1,
                   "hidden": 1, "read": 1}, 45)
        reachable.update(p["view"] for p in parts if p["view"])

    missing = {v.key for v in jobs.VIEWS} - reachable
    assert not missing, f"unreachable from the screen: {missing}"


async def test_a_pile_number_opens_the_pile_it_counts(user: User, con, data_dir):
    _job(con, "a", "Gut GmbH", 80)
    _job(con, "b", "Schlecht GmbH", 0)

    await user.open("/")
    await user.should_see("1 passen nicht")
    user.find(marker="pile-passt_nicht").click()
    await asyncio.sleep(0.3)

    assert _row_companies(user) == ["Schlecht GmbH"]


async def test_the_line_says_once_that_nothing_is_deleted(user: User, con,
                                                          data_dir):
    """Instead of "ausgeblendet" after every number — five of them in one line
    is the noise he rejected the arrangement over."""
    _job(con, "a", "Gut GmbH", 80)
    _job(con, "b", "Schlecht GmbH", 0)

    await user.open("/")

    await user.should_see("Nichts wird gelöscht.")


async def test_the_control_names_the_view_that_is_on_screen(user: User, con,
                                                            data_dir):
    """Found only by driving the real screen: opening a pile from its number
    left the control reading "Neu" while the list showed the offline pile — a
    control naming a different view from the one on screen is a control that
    lies, and it was also the way back out."""
    _job(con, "a", "Gut GmbH", 80)
    _job(con, "b", "Schlecht GmbH", 0)
    await user.open("/")

    user.find(marker="pile-passt_nicht").click()
    await asyncio.sleep(0.3)

    control = _marked(user, "view-select")[0]
    assert control.value == "passt_nicht"
    assert "passt_nicht" in control.options
    # …and the four main ways of looking are still offered, so it is a way out
    assert set(jobs.MAIN_VIEW_KEYS) <= set(control.options)


async def test_the_control_drops_the_pile_again_once_he_leaves_it(
        user: User, con, data_dir):
    """It carries the CURRENT view, not every view he has visited — otherwise
    the eleven-item dropdown grows back one press at a time."""
    _job(con, "a", "Gut GmbH", 80)
    _job(con, "b", "Schlecht GmbH", 0)
    await user.open("/")
    user.find(marker="pile-passt_nicht").click()
    await asyncio.sleep(0.3)

    _marked(user, "view-select")[0].set_value("offen")
    await asyncio.sleep(0.3)

    control = _marked(user, "view-select")[0]
    assert control.value == "offen"
    assert "passt_nicht" not in control.options
