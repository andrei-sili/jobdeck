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
from jobdeck.ai import drafting as ai_drafting
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


def _job(con, ext, description, *, score=80, reason="Passt gut.",
         company="Eine GmbH"):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": ext, "title": "Entwickler",
        "company": company, "url": f"https://example.invalid/{ext}",
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


def _row_metas(user: User) -> list[str]:
    """The meta line of every rendered LIST row.

    `row_meta` was exercised only dict-in/string-out, so neither draw site was
    covered and nothing pinned that the loader hands `description` to the row
    at all — the marker rested entirely on the list query being SELECT *."""
    with user.client:
        rows = [el for el in user.client.elements.values()
                if "jd-row" in getattr(el, "_classes", [])]
        out = []
        for row in rows:
            out += [d.text for d in row.descendants()
                    if isinstance(d, ui.label)
                    and "jd-meta" in getattr(d, "_classes", [])]
        return out


def _markdown(user: User) -> list[str]:
    """EVERY rendered markdown body, whatever class it carries.

    `_adverts` is keyed on the exact class string, so an advert body drawn
    under a different class would slip past it — and the placeholder this
    slice removed was rendered through markdown, which means a label-only
    assertion could never have observed it in the first place."""
    with user.client:
        return [el.content for el in user.client.elements.values()
                if isinstance(el, ui.markdown)]


# --------------------------------------------------- what the sentences say


def test_a_posting_with_no_advert_names_what_a_letter_from_it_would_be():
    """The second sentence is the one that matters. A letter written from no
    advert can only restate the profile — verified on a real posting, where
    the result answered not one requirement of the role."""
    note = jobs.missing_text_note({"url": "https://example.invalid/1"})
    assert note.startswith("Für diese Anzeige ist kein Text gespeichert.")
    assert "nur dein Profil wiederholen" in note
    assert "es geht auf keine einzige Anforderung der Stelle ein" in note
    assert "Der vollständige Text steht beim Anbieter." in note


def test_a_posting_nobody_can_open_does_not_send_him_to_a_page():
    """"Der vollständige Text steht beim Anbieter" over a posting with no
    openable link is an instruction he cannot follow."""
    assert "Anbieter" not in jobs.missing_text_note({})
    assert "Anbieter" not in jobs.missing_text_note({"url": "javascript:x"})


@pytest.mark.parametrize("description, expected", [
    (FULL, ""),
    ("", "Diese Bewertung entstand ohne Anzeigentext — beurteilt wurden "
         "nur Titel, Firma und Ort."),
    (SNIPPET, "Diese Bewertung beruht nur auf dem Ausschnitt oben, nicht "
              "auf der vollständigen Anzeige."),
])
def test_the_verdict_says_how_much_there_was_to_read(description, expected):
    assert jobs.verdict_caveat(
        {"match_score": 70, "description": description}) == expected


def test_a_score_of_zero_is_the_verdict_that_most_needs_qualifying():
    """`match_score is None` is the guard, and the tidy-up anyone would make
    to that line — `if not job.get("match_score")` — silences every score-0
    posting with the suite green. Those are the knock-outs: the verdict that
    files a posting away as a mismatch, and a 0 formed from a title alone is
    exactly the one worth saying so about."""
    assert jobs.verdict_caveat({"match_score": 0, "description": ""}) \
        == ("Diese Bewertung entstand ohne Anzeigentext — beurteilt wurden "
            "nur Titel, Firma und Ort.")


def test_a_posting_the_source_says_is_gone_is_not_sent_after_its_text():
    """The pane already draws "⚠ Anzeige offline — beim letzten Abruf … nicht
    mehr vorhanden" a few blocks higher. Adding "der vollständige Text steht
    beim Anbieter" underneath it is the app contradicting itself on one
    screen, and the no-text pile is where the expired postings live."""
    alive = {"url": "https://example.invalid/1", "liveness": "alive"}
    gone = {"url": "https://example.invalid/1", "liveness": "gone"}
    assert "beim Anbieter" in jobs.missing_text_note(alive)
    assert "beim Anbieter" not in jobs.missing_text_note(gone)
    # …and the sentence it keeps is the one that is still true
    assert jobs.missing_text_note(gone).startswith(
        "Für diese Anzeige ist kein Text gespeichert.")


def test_the_reading_pane_header_does_not_repeat_the_list_marker():
    """The row part was written for the LIST, where it is the only thing that
    tells a title-only score from an advert-based one. In the pane the advert
    itself is a few centimetres below and says the same thing in full — the
    same duplication `_score_line(with_age=False)` already exists to avoid."""
    job = {"match_score": 82, "age_days": 2, "apply_channel": "",
           "source": "jooble", "description": SNIPPET}
    assert "nur ein Ausschnitt" in jobs.row_meta(job)
    assert "nur ein Ausschnitt" not in jobs.row_meta(job, with_text_state=False)
    # everything else the line states survives the flag
    assert jobs.row_meta(job, with_text_state=False) == "2 T · Kanal offen · Jooble"


def test_an_ungraded_posting_gets_no_caveat_about_its_grade():
    """There is no verdict to qualify, and the pane already says a grade is
    coming — a second sentence there would answer a question nobody asked."""
    assert jobs.verdict_caveat({"match_score": None, "description": ""}) == ""


# ------------------------------------------------------------- on the screen


async def test_the_list_row_carries_the_marker_the_loader_really_hands_it(
        user: User, con, data_dir):
    """Three shapes on one screen, so the pane cannot describe a different row
    than the one being asserted and the marker cannot be a coincidence. This
    also pins that the loader supplies `description` at all: the module reads
    an absent key as "no text" on purpose, which fails loudly on his screen
    and silently in CI — and CI is the half that decides whether it ships."""
    # one row stands for a COMPANY, so three shapes need three employers
    _job(con, "empty", "", score=85, reason="Titel passt genau.", company="Alpha GmbH")
    _job(con, "snip", SNIPPET, score=82, reason="Fragment.", company="Beta GmbH")
    _job(con, "full", FULL, score=80, reason="Ganze Anzeige.", company="Gamma GmbH")

    await user.open("/")

    metas = _row_metas(user)
    assert len(metas) == 3, metas
    marked = [m for m in metas if "Anzeigentext" in m or "Ausschnitt" in m]
    assert len(marked) == 2, metas
    assert any(m.startswith("kein Anzeigentext · ") for m in metas), metas
    assert any(m.startswith("nur ein Ausschnitt · ") for m in metas), metas
    # …and exactly one row says nothing about its text, because it has all of it
    assert sum("Anzeigentext" not in m and "Ausschnitt" not in m
               for m in metas) == 1, metas


async def test_a_sibling_posting_says_what_its_score_stands_on_too(
        user: User, con, data_dir):
    """The sibling panel exists so he can choose WHICH posting to apply with,
    and the score is the whole basis of that choice — so a sibling graded on a
    title alone must not render like one graded on four thousand characters.
    Score is also the within-company ranking key, which is what puts an
    inflated title-only score at the top of this very list."""
    _job(con, "best", FULL, score=90, reason="Ganze Anzeige.")
    _job(con, "thin", "", score=85, reason="Titel passt genau.")

    await user.open("/")

    labels = _labels(user)
    assert any("weitere Stelle bei Eine GmbH" in text for text in labels), labels
    # the sibling is the text-less one, and it says so beside its score
    assert labels.count("kein Anzeigentext") == 1, \
        "the sibling's score is rendered with no word about what it stands on"


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
    # …and no markdown body under ANY class: the placeholder was rendered
    # through ui.markdown, so an assertion over labels could never have seen
    # it, and one keyed on the advert's CSS class would miss it under another.
    assert not any("keine Beschreibung" in body for body in _markdown(user))
    labels = _labels(user)
    assert any("kein Text gespeichert" in text for text in labels)
    assert any("nur dein Profil wiederholen" in text for text in labels)


async def test_a_grade_formed_without_the_advert_says_so_under_its_reason(
        user: User, con, data_dir):
    _job(con, "empty", "", score=85, reason="Titel passt genau.")

    await user.open("/")

    labels = _labels(user)
    assert "Titel passt genau." in labels
    assert any("ohne Anzeigentext" in text for text in labels)


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
    assert any("beruht nur auf dem Ausschnitt oben" in text for text in labels)
    assert not any("kein Text gespeichert" in text for text in labels)
    # ONCE on the screen, not twice: the pane's header reuses row_meta, and
    # the advert it describes is a few centimetres below. Asserted at the DRAW
    # site — the pure-function test two blocks up passes either way, so
    # dropping the flag at the call site would go unnoticed without this.
    assert sum("nur ein Ausschnitt" in text for text in labels) == 1, labels


async def test_a_verdict_with_no_prose_still_says_what_it_stands_on(
        user: User, con, data_dir):
    """The caveat used to hang off the reason paragraph, so a score stored
    with an empty reason rendered neither arm of the verdict block and lost
    the caveat exactly where the pane already says least."""
    _job(con, "empty", "", score=85, reason="")

    await user.open("/")

    labels = _labels(user)
    assert any("ohne Anzeigentext" in text for text in labels), labels
    assert any("WARUM 85" in text for text in labels), labels


def test_an_advert_arriving_moves_the_page_signature(con):
    """Three statements on this screen are derived from `jobs.description`,
    and a watcher that cannot see the column can never take any of them back.
    Nothing writes a description in place today — every `UPDATE jobs` was read
    and `set_job_contacts` has a closed allowlist without it — so this is the
    guard that has to exist BEFORE the first thing that does, which is the
    description backfill already on the table."""
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "e1", "title": "Entwickler",
        "company": "Eine GmbH", "url": "https://example.invalid/1",
        "description": "",
    })
    db.set_job_score(con, job_id, 85, "Titel passt genau.")
    con.commit()
    before = jobs.signature_of(con)

    con.execute("UPDATE jobs SET description=? WHERE id=?", (FULL, job_id))
    con.commit()
    assert jobs.signature_of(con) != before, \
        "an advert arriving has to move it, or the row keeps saying there is none"

    # …and a REPLACED advert too: a count alone cannot see one text swapped
    # for another of a different length.
    arrived = jobs.signature_of(con)
    con.execute("UPDATE jobs SET description=? WHERE id=?",
                (FULL + " Und noch ein Satz.", job_id))
    con.commit()
    assert jobs.signature_of(con) != arrived


def test_the_screen_and_both_prompts_read_the_same_function():
    """One home for "how much is there". A row, a reading pane, the scoring
    prompt and the LETTER prompt that each decided for themselves would each
    be honest alone and collectively a lie — so all four are driven here
    against the same inputs. The letter half is the one that decides what he
    sends, and before this test nothing pinned it to the shared function at
    all."""
    job = {"title": "Entwickler", "company": "Eine GmbH", "location": "Berlin",
           "remote": 0, "refnr": "K-17", "ansprechpartner": ""}
    for description in ("", "   ", SNIPPET, FULL):
        state = scoring.posting_text_state(description)
        row = jobs.row_meta({"match_score": 70, "age_days": 1,
                             "apply_channel": "", "source": "",
                             "description": description})
        caveat = jobs.verdict_caveat({"match_score": 70,
                                      "description": description})
        scored = scoring.build_user_content({**job, "description": description},
                                            "profile")
        letter = ai_drafting.build_user_content(
            {**job, "description": description}, "profile",
            refnr="K-17", applicant_name="Erika Muster")
        assert ("kein Anzeigentext" in row) == (state == scoring.TEXT_NONE)
        assert ("nur ein Ausschnitt" in row) == (state == scoring.TEXT_SNIPPET)
        assert bool(caveat) == (state != scoring.TEXT_FULL)
        for prompt in (scored, letter):
            assert ("NO advert text is available" in prompt) \
                == (state == scoring.TEXT_NONE)
            assert ("SEARCH-RESULT SNIPPET" in prompt) \
                == (state == scoring.TEXT_SNIPPET)
