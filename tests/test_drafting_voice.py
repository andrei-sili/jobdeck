"""The letter has to read as though a person wrote it.

The prompt asks for no dashes, but a prompt rule is a probability. These pin
the guarantee that survives a model in a bad mood — and the one case where a
dash must be left exactly where it is.
"""

import pytest

from jobdeck.ai import drafting


def _response(anschreiben: str, email: str = "Guten Tag,",
              titel: str = "Python Entwickler (m/w/d)") -> str:
    return (
        "===ANALYSIS===\nnotes\n"
        f"===STELLENBEZEICHNUNG===\n{titel}\n"
        f"===ANSCHREIBEN_BODY===\n{anschreiben}\n"
        f"===EMAIL_BODY===\n{email}\n"
        "===END===\n"
    )


@pytest.mark.parametrize("dash", ["—", "–"])
def test_a_dash_joined_afterthought_becomes_the_comma_it_always_was(dash):
    """Measured on nine real letters written in one batch: seven carried at
    least one, all of them in this shape."""
    body = f"Bei der Beispiel GmbH habe ich mitgearbeitet {dash} im Team an einer API."

    out = drafting.parse_draft_sections(_response(body))

    assert out["anschreiben_body"] == (
        "Bei der Beispiel GmbH habe ich mitgearbeitet, im Team an einer API.")
    assert dash not in out["anschreiben_body"]


def test_the_e_mail_is_cleaned_too():
    out = drafting.parse_draft_sections(
        _response("Sehr geehrte Damen und Herren,",
                  "Ihre Stelle passt — genau das mache ich."))

    assert out["email_body"] == "Ihre Stelle passt, genau das mache ich."


def test_a_dash_inside_the_job_title_is_left_alone():
    """The posting's own Stellenbezeichnung can carry one ("Data Platform
    Engineer – Data Operations"). Turning that into a comma would rename the
    position being applied for, which HR matches on."""
    titel = "Data Platform Engineer – Data Operations (all genders)"
    body = f"Für die Position als {titel} bringe ich Erfahrung mit."

    out = drafting.parse_draft_sections(_response(body, titel=titel))

    assert titel in out["anschreiben_body"]


def test_a_hyphenated_compound_survives():
    body = "Als Java-Entwickler arbeite ich mit Spring-Boot-Anwendungen."

    out = drafting.parse_draft_sections(_response(body))

    assert out["anschreiben_body"] == body


def test_the_prompt_itself_carries_no_dash_to_copy():
    """The model mirrors the register of the text instructing it: the prompt
    held twenty em dashes while asking for prose, and seven of nine letters
    came back dashed."""
    assert "—" not in drafting.SYSTEM_PROMPT
    assert "–" not in drafting.SYSTEM_PROMPT


def test_the_prompt_asks_for_it_as_well_as_enforcing_it():
    """Belt and braces on purpose: the cleanup guarantees the output, the
    rule keeps the model from writing sentences built around a dash in the
    first place — a comma dropped in afterwards cannot fix the rhythm."""
    assert "NEVER use an em dash" in drafting.SYSTEM_PROMPT
