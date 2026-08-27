"""The screen for entering an advert the user found themselves.

Driven through the real page, because the whole slice is a route into the
product: a service that works behind a button nobody can reach would leave the
sqlite prompt as the only way in, which is the behaviour this ends.
"""

import asyncio
import sys

import pytest
from nicegui.testing import User

from jobdeck import db
from jobdeck.services import manual_posting, polling
from jobdeck.ui.pages import jobs as jobs_page

pytest_plugins = ["nicegui.testing.user_plugin"]

pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_main.py")


@pytest.fixture(autouse=True)
def _keep_the_package_importable():
    """See test_draft_visibility_pages: NiceGUI's reset pops `jobdeck` itself
    out of sys.modules and breaks monkeypatching for the rest of the session."""
    saved = {name: mod for name, mod in sys.modules.items()
             if name == "jobdeck" or name.startswith("jobdeck.")}
    yield
    sys.modules.update(saved)


async def _fill(user: User, **fields) -> None:
    for marker, value in fields.items():
        element = next(iter(user.find(marker=f"manual-{marker}").elements))
        element.set_value(value)
    await asyncio.sleep(0.05)


async def _open_dialog(user: User) -> None:
    await user.open("/")
    user.find(marker="add-posting").click()
    await asyncio.sleep(0.1)


# ------------------------------------------------------------------ the door


async def test_the_screen_offers_a_way_to_add_a_posting(user: User, con, data_dir):
    """Without this control the only way in was raw SQL, reached for four
    times — walking past the duplicate check and the cooling-off window."""
    await user.open("/")
    await user.should_see(marker="add-posting")


async def test_the_dialog_names_what_it_needs(user: User, con, data_dir):
    await _open_dialog(user)
    await user.should_see("Anzeige hinzufügen")
    await user.should_see(marker="manual-url")
    await user.should_see(marker="manual-company")
    await user.should_see(marker="manual-title")
    await user.should_see(marker="manual-text")


async def test_the_dialog_states_the_cost_of_leaving_the_text_empty(
        user: User, con, data_dir):
    """The same sentence the reading pane shows afterwards, said where the
    decision is actually made."""
    await _open_dialog(user)
    await user.should_see("nur dein Profil wiederholen")


async def test_the_dialog_says_the_score_is_not_his_to_type(
        user: User, con, data_dir, monkeypatch):
    """Every row he entered by hand carried a score and a reason he typed."""
    monkeypatch.setattr(jobs_page.scoring_service, "is_ready", lambda con: True)
    await _open_dialog(user)
    await user.should_see("Die Bewertung macht JobDeck selbst")


async def test_the_dialog_does_not_promise_a_score_that_is_not_coming(
        user: User, con, data_dir, monkeypatch):
    """Same rule as `unscored_note`: promise only what will be kept. Scoring
    OFF is this app's own default, and the first version of this dialog
    promised unconditionally — the row would have waited for ever under a
    word that says a worker is coming for it."""
    monkeypatch.setattr(jobs_page.scoring_service, "is_ready", lambda con: False)
    await _open_dialog(user)
    await user.should_see("wenn du die Bewertung in den Einstellungen "
                          "einschaltest")


def test_the_scoring_note_and_the_outcome_agree_about_the_same_gate():
    """Two sentences, one fact. They are written in different places and shown
    a minute apart, which is exactly how they drift."""
    assert "macht JobDeck selbst" in jobs_page.manual_scoring_note(True)
    assert "einschaltest" in jobs_page.manual_scoring_note(False)
    ready = jobs_page.manual_outcome_line(
        polling.Stored(polling.NEW, 7), "F", scoring_ready=True)
    blocked = jobs_page.manual_outcome_line(
        polling.Stored(polling.NEW, 7), "F", scoring_ready=False)
    assert "folgt automatisch" in ready
    assert "folgt automatisch" not in blocked
    assert "einschaltest" in blocked


# ------------------------------------------------------------------ writing


async def test_a_pasted_advert_reaches_the_list(user: User, con, data_dir):
    await _open_dialog(user)
    await _fill(user, company="Beispiel GmbH", title="Junior Backend",
                text="Wir suchen dich fuer Django.")
    user.find(marker="manual-save").click()
    await asyncio.sleep(0.4)

    row = con.execute(
        "SELECT * FROM jobs WHERE company='Beispiel GmbH'").fetchone()
    assert row is not None
    assert row["title"] == "Junior Backend"
    assert "Django" in row["description"]
    assert row["match_score"] is None      # the scorer judges it, not him
    assert row["profile_id"] is None       # it came from no search profile


async def test_a_missing_company_is_refused_with_the_reason(
        user: User, con, data_dir):
    await _open_dialog(user)
    await _fill(user, title="Junior Backend", text="Text")
    user.find(marker="manual-save").click()
    await asyncio.sleep(0.3)
    await user.should_see("Ohne Firma geht es nicht")
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


async def test_a_missing_title_is_refused_with_the_reason(
        user: User, con, data_dir):
    await _open_dialog(user)
    await _fill(user, company="Beispiel GmbH", text="Text")
    user.find(marker="manual-save").click()
    await asyncio.sleep(0.3)
    await user.should_see("Ohne Stellenbezeichnung geht es nicht")
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


async def test_an_advert_already_in_the_list_is_not_doubled(
        user: User, con, data_dir):
    db.insert_job_if_new(con, {
        "profile_id": None, "source": "arbeitsagentur", "external_id": "x-1",
        "title": "Junior Backend", "company": "Beispiel GmbH",
    })
    con.commit()
    await _open_dialog(user)
    await _fill(user, company="Beispiel GmbH", title="Junior Backend",
                text="Text")
    user.find(marker="manual-save").click()
    await asyncio.sleep(0.3)
    await user.should_see("steht schon in der Liste")
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


# ------------------------------------------------- what the outcome line says


def test_the_outcome_line_names_the_rule_that_turned_it_away():
    """A silent no-op on a posting he just typed out is what would send him
    back to the sqlite prompt, so each refusal says WHICH rule applied."""
    known = jobs_page.manual_outcome_line(
        polling.Stored(polling.KNOWN, None), "Beispiel GmbH")
    assert "schon in der Liste" in known

    dupe = jobs_page.manual_outcome_line(
        polling.Stored(polling.DUPLICATE, 7), "Beispiel GmbH")
    assert "Beispiel GmbH" in dupe
    # the view is called «Doppelt»; «Schon beworben» is not a view this app
    # has, and a name that is nearly right sends him looking in the wrong place
    assert "Doppelt" in dupe
    assert "Schon beworben" not in dupe

    added = jobs_page.manual_outcome_line(polling.Stored(polling.NEW, 7), "X")
    assert "aufgenommen" in added.lower()


def test_an_old_advert_says_which_pile_it_landed_in():
    """A fetched advert carries the board's real publication date, so one
    posted eight weeks ago lands straight in the age pile — correct, and
    invisible on the screen that just confirmed taking it. Verified live: an
    advert added from a link was 48 days old and vanished from the list."""
    line = jobs_page.manual_outcome_line(
        polling.Stored(polling.NEW, 7), "Firma", age_days=48,
        stale_age_days=30, landed="old")
    assert "aufgenommen" in line.lower()
    assert "48 Tage alt" in line
    # the view is called «Alt»; naming it anything else sends him looking in a
    # control that does not offer that word
    assert "«Alt»" in line


def test_a_fresh_advert_says_nothing_about_piles():
    """The note is only true where it is true. A line that always appears is a
    line he stops reading."""
    line = jobs_page.manual_outcome_line(
        polling.Stored(polling.NEW, 7), "Firma", age_days=3, stale_age_days=30)
    assert "liegt allerdings" not in line


def test_an_advert_with_no_date_claims_nothing_about_its_age():
    line = jobs_page.manual_outcome_line(
        polling.Stored(polling.NEW, 7), "Firma", age_days=None,
        stale_age_days=30, landed="old")
    assert "Tage alt" not in line


@pytest.mark.parametrize("landed", sorted(jobs_page._LANDING_VIEW_KEYS))
def test_every_pile_is_named_with_a_label_the_view_control_offers(landed):
    """The sentence tells him where to look. Two of these were spelled by hand
    and both were wrong — «Schon beworben» is not a view this app has, and the
    age view is called «Alt», not «Ältere Anzeigen»."""
    label = jobs_page.landing_view_label(landed)
    assert label, f"{landed} names no view"
    assert label in [v.label for v in jobs_page.VIEWS]


def test_a_pile_that_is_not_one_names_nothing():
    assert jobs_page.landing_view_label("") == ""
    assert jobs_page.landing_view_label("something-else") == ""


def test_a_refused_advert_never_talks_about_piles():
    """It was not taken, so there is no row anywhere to point at."""
    for outcome in (polling.KNOWN, polling.DUPLICATE):
        line = jobs_page.manual_outcome_line(
            polling.Stored(outcome, None), "Firma", age_days=99,
            stale_age_days=30, landed="old")
        assert "99 Tage alt" not in line


def test_every_refusal_the_service_can_return_has_a_sentence():
    """A refusal with no message renders an empty warning — the exact silent
    failure that sends someone to raw SQL."""
    for refusal in (manual_posting.NEEDS_COMPANY, manual_posting.NEEDS_TITLE):
        assert jobs_page.MANUAL_REFUSALS.get(refusal, "").strip()
