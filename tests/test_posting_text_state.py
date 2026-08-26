"""An advert JobDeck does not have, and how the screen says so.

Three of its own postings show the shape. One was stored the morning this was
written: the source answered the search and refused the detail call, so the
posting arrived with no text — and was then graded 15, with a reason worded
like a reading of an advert nobody had. Measured over 1521 stored postings, 27
hold no text and 178 hold only an elided search fragment; every one carries a
grade, and nothing on any screen said the grade was formed on less.

The property under test is not the wording. It is that a posting the app knows
nothing about must never look like one it knows everything about.
"""

import datetime
import sys

import pytest
from nicegui import ui
from nicegui.testing import User

from jobdeck import db
from jobdeck.ai import scoring
from jobdeck.ui.pages import jobs

pytest_plugins = ["nicegui.testing.user_plugin"]

pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_main.py")

SNIPPET = "Deine Mission - Du entwickelst das Herz unserer Services. Als..."
FULL = "Wir suchen eine Python-Entwicklerin. Django, FastAPI, PostgreSQL. " * 12


@pytest.fixture(autouse=True)
def _keep_the_package_importable():
    saved = {name: mod for name, mod in sys.modules.items()
             if name == "jobdeck" or name.startswith("jobdeck.")}
    yield
    sys.modules.update(saved)


def _job(con, ext, description, *, score=80, reason="Passt gut."):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": ext, "title": "Entwickler",
        "company": "Eine GmbH", "url": f"https://example.invalid/{ext}",
        "description": description,
    })
    day = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    con.execute("UPDATE jobs SET published_on=? WHERE id=?", (day, job_id))
    if score is not None:
        db.set_job_score(con, job_id, score, reason)
    con.commit()
    return job_id


def _labels(user: User) -> list[str]:
    with user.client:
        return [el.text for el in user.client.elements.values()
                if isinstance(el, ui.label) and el.text]


def _adverts(user: User) -> list[str]:
    """The rendered advert bodies — the markdown block, not the notes."""
    with user.client:
        return [el.content for el in user.client.elements.values()
                if isinstance(el, ui.markdown)
                and "jd-ad" in getattr(el, "_classes", [])]


# --------------------------------------------------- what the sentences say


def test_a_posting_with_no_advert_names_what_a_letter_from_it_would_be():
    """The second sentence is the one that matters. A letter written from no
    advert can only restate the profile — verified on a real posting, where
    the result answered not one requirement of the role."""
    note = jobs.missing_text_note({"url": "https://example.invalid/1"})
    assert note.startswith("Für diese Anzeige ist kein Text gespeichert.")
    assert "nur dein Profil wiederholen" in note
    assert "Der vollständige Text steht beim Anbieter." in note


def test_a_posting_nobody_can_open_does_not_send_him_to_a_page():
    """"Der vollständige Text steht beim Anbieter" over a posting with no
    openable link is an instruction he cannot follow."""
    assert "Anbieter" not in jobs.missing_text_note({})
    assert "Anbieter" not in jobs.missing_text_note({"url": "javascript:x"})


@pytest.mark.parametrize("description, expected", [
    (FULL, ""),
    ("", "Diese Bewertung entstand ohne den Anzeigentext — beurteilt wurden "
         "nur Titel, Firma und Ort."),
    (SNIPPET, "Diese Bewertung entstand nur auf dem Ausschnitt oben, nicht "
              "auf der vollständigen Anzeige."),
])
def test_the_verdict_says_how_much_there_was_to_read(description, expected):
    assert jobs.verdict_caveat(
        {"match_score": 70, "description": description}) == expected


def test_an_ungraded_posting_gets_no_caveat_about_its_grade():
    """There is no verdict to qualify, and the pane already says a grade is
    coming — a second sentence there would answer a question nobody asked."""
    assert jobs.verdict_caveat({"match_score": None, "description": ""}) == ""


# ------------------------------------------------------------- on the screen


async def test_the_reading_pane_states_a_missing_advert_instead_of_drawing_one(
        user: User, con, data_dir):
    """It used to render "(keine Beschreibung)" as the advert body, then a
    verdict and a row of buttons underneath — the posting the app knew nothing
    about, styled exactly like one it knew everything about."""
    # 85 on purpose: the postings this hurts are the ones that rank HIGH on a
    # title alone, and the list's score floor hides the low ones anyway.
    _job(con, "empty", "", score=85, reason="Titel passt genau.")

    await user.open("/")

    assert _adverts(user) == [], "an advert body was drawn where none exists"
    labels = _labels(user)
    assert any("kein Text gespeichert" in text for text in labels)
    assert any("nur dein Profil wiederholen" in text for text in labels)
    assert not any("keine Beschreibung" in text for text in labels)


async def test_a_grade_formed_without_the_advert_says_so_under_its_reason(
        user: User, con, data_dir):
    _job(con, "empty", "", score=85, reason="Titel passt genau.")

    await user.open("/")

    labels = _labels(user)
    assert "Titel passt genau." in labels
    assert any("ohne den Anzeigentext" in text for text in labels)


async def test_a_whole_advert_is_drawn_and_carries_no_caveat_at_all(
        user: User, con, data_dir):
    """The silence is the point: 273 of 314 rows on the working list hold a
    whole advert, and a caveat on all of them would be read past."""
    _job(con, "full", FULL)

    await user.open("/")

    assert len(_adverts(user)) == 1
    assert FULL.strip()[:40] in _adverts(user)[0]
    labels = _labels(user)
    assert not any("kein Text gespeichert" in text for text in labels)
    assert not any("Anzeigentext" in text for text in labels)
    assert not any("Ausschnitt" in text for text in labels)


async def test_a_fragment_is_drawn_and_declared_a_fragment(
        user: User, con, data_dir):
    """Some of the advert is not none of it: the text is shown, and both the
    note under it and the caveat under the verdict say it is a fragment."""
    _job(con, "snip", SNIPPET)

    await user.open("/")

    assert len(_adverts(user)) == 1
    labels = _labels(user)
    assert any("nur einen Ausschnitt" in text for text in labels)
    assert any("nur auf dem Ausschnitt oben" in text for text in labels)
    assert not any("kein Text gespeichert" in text for text in labels)


def test_the_screen_and_the_prompt_read_the_same_function():
    """One home for "how much is there". A row, a reading pane and a prompt
    that each decided for themselves would each be honest alone and
    collectively a lie."""
    for description in ("", "   ", SNIPPET, FULL):
        state = scoring.posting_text_state(description)
        row = jobs.row_meta({"match_score": 70, "age_days": 1,
                             "apply_channel": "", "source": "",
                             "description": description})
        caveat = jobs.verdict_caveat({"match_score": 70,
                                      "description": description})
        assert ("kein Anzeigentext" in row) == (state == scoring.TEXT_NONE)
        assert ("nur ein Ausschnitt" in row) == (state == scoring.TEXT_SNIPPET)
        assert bool(caveat) == (state != scoring.TEXT_FULL)
