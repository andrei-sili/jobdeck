"""Every source a row can carry has a name on every screen that prints one.

Three maps hold this knowledge for three different purposes — a short tag on
the list row, a name for a panel, and the full legal name an employer's form
asks for. They can drift, and a missed entry falls through to the lowercase
adapter identifier, which is how `manual` would have appeared beside `BA` and
`Jooble` in an otherwise German screen.
"""

import pytest

from jobdeck import apply_form
from jobdeck.services import register
from jobdeck.services.manual_posting import MANUAL_SOURCE
from jobdeck.sources import get_sources
from jobdeck.ui.pages import jobs as jobs_page


def _known_sources() -> set[str]:
    """Every value that can end up in `jobs.source`: the adapters, plus the
    one a posting the user entered himself carries."""
    return set(get_sources(client=None)) | {MANUAL_SOURCE}


@pytest.mark.parametrize("source", sorted(_known_sources()))
def test_the_list_row_names_every_source(source):
    label = jobs_page._SOURCE_LABELS.get(source, "")
    assert label and label != source


@pytest.mark.parametrize("source", sorted(_known_sources()))
def test_the_panel_names_every_source(source):
    assert register.source_name(source) != source


def test_a_hand_entered_posting_says_so_rather_than_naming_a_board():
    assert jobs_page._SOURCE_LABELS[MANUAL_SOURCE] == "von dir"
    assert "eingetragen" in register.source_name(MANUAL_SOURCE)


def test_an_internal_key_never_reaches_an_employers_form():
    """The value is copied into the employer's own form. `manual` is not an
    answer to "Wie haben Sie von uns erfahren?", and neither is an adapter name
    for a source nobody added a legal name for."""
    fields = {f.label: f for f in apply_form.posting_fields(
        {"source": MANUAL_SOURCE, "title": "T"}, None)}
    found_via = fields["Gefunden über"]
    assert found_via.value == ""
    assert "enter where you found" in found_via.hint


@pytest.mark.parametrize("source", sorted(_known_sources() - {MANUAL_SOURCE}))
def test_the_employers_form_names_every_real_board(source):
    """The third map, and the one whose value is copied into a real employer's
    form. The module claimed all three were guarded against drift while only
    two were parametrized."""
    assert apply_form.SOURCE_LABELS.get(source, "").strip(), \
        f"{source} has no legal name for the 'Wie haben Sie von uns erfahren?' field"


def test_an_unregistered_board_still_answers_with_its_own_name():
    """Deliberate, and older than this slice: `neuesboard` is very nearly the
    board's name and he can recognise it, which beats a blank. Only a posting
    he entered HIMSELF has no board to name."""
    fields = {f.label: f for f in apply_form.posting_fields(
        {"source": "some-new-board", "title": "T"}, None)}
    assert fields["Gefunden über"].value == "some-new-board"
