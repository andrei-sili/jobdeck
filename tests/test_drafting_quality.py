"""The one re-roll on what a person would hold against the letter.

A prompt rule is a probability. When the sample still carries a stock
phrase, a sentence from the advert or an opening already used, the drafting
loop asks once more — once, because each sample is a paid Sonnet call — and
then accepts what comes back, leaving the rest to the Postausgang.
"""

from jobdeck.ai import drafting, llm


def _response(anschreiben: str) -> str:
    return (
        "===ANALYSIS===\nnotes\n"
        "===STELLENBEZEICHNUNG===\nPython Entwickler (m/w/d)\n"
        f"===ANSCHREIBEN_BODY===\n{anschreiben}\n"
        "===EMAIL_BODY===\nGuten Tag,\n\nanbei.\n\nMit freundlichen Grüßen\nErika\n"
        "===END===\n"
    )


JOB = {"id": 7, "title": "Python Entwickler (m/w/d)", "company": "Firma GmbH",
       "location": "Köln", "remote": 0, "ansprechpartner": "",
       "description": "Wir suchen Python und Django. Sie entwickeln REST-APIs "
                      "mit Python und Django REST Framework für unsere Kunden."}

CLEAN = "Sehr geehrte Damen und Herren,\n\nbei Beispiel GmbH habe ich Endpunkte gebaut."
FLOSKEL = ("Sehr geehrte Damen und Herren,\n\nDie Aufgabe reizt mich besonders, "
           "bei Beispiel GmbH habe ich Endpunkte gebaut.")


def _fake(samples: list[str], seen: list[str]):
    def complete(**kwargs):
        seen.append(kwargs["user_content"])
        text = samples.pop(0)
        return llm.LLMResult(text=text, model="m", input_tokens=1,
                             output_tokens=1, cost_usd=0.01)
    return complete


def test_a_stock_phrase_costs_exactly_one_more_sample(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(llm, "complete",
                        _fake([_response(FLOSKEL), _response(CLEAN)], seen))

    body, _mail, _titel, usage = drafting.draft_application(JOB, "profile")

    assert body == CLEAN
    assert len(seen) == 2
    # the second request names the phrase to avoid, on top of the same content
    assert seen[1].startswith(seen[0])
    assert "reizt mich besonders" in seen[1]
    # both samples were paid for and both are metered
    assert usage.cost_usd == 0.02


def test_a_phrase_that_survives_the_re_roll_is_accepted_not_paid_for_again(
    monkeypatch
):
    seen: list[str] = []
    monkeypatch.setattr(llm, "complete",
                        _fake([_response(FLOSKEL), _response(FLOSKEL),
                               _response(CLEAN)], seen))

    body, *_ = drafting.draft_application(JOB, "profile")

    assert body == FLOSKEL          # what came back, for the Postausgang to flag
    assert len(seen) == 2


def test_a_clean_first_sample_is_not_re_rolled(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(llm, "complete", _fake([_response(CLEAN)], seen))

    drafting.draft_application(JOB, "profile")

    assert len(seen) == 1


def test_an_opening_used_before_is_re_rolled_too(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(llm, "complete",
                        _fake([_response(CLEAN), _response(
                            "Sehr geehrte Damen und Herren,\n\nMein Weg in die "
                            "IT begann mit einem Praktikum.")], seen))
    earlier = "Sehr geehrte Frau Weber,\n\nBei Beispiel GmbH habe ich Endpunkte gebaut."

    body, *_ = drafting.draft_application(JOB, "profile",
                                          previous_letters=[earlier])

    assert body.startswith("Sehr geehrte Damen und Herren,\n\nMein Weg")
    assert "same opening" in seen[1]


def test_the_prompt_forbids_the_phrases_and_asks_for_the_adverts_terms():
    """The rules the guards enforce are asked for first — the guard is the
    net, not the instruction."""
    prompt = " ".join(drafting.SYSTEM_PROMPT.split())
    for phrase in ("reizt mich besonders", "mit großem Interesse",
                   "In der heutigen dynamischen Arbeitswelt"):
        assert phrase in prompt
    assert "advert's TERM" in drafting.SYSTEM_PROMPT
    assert "correct German orthography" in drafting.SYSTEM_PROMPT
    assert "Mirror TERMS, never SENTENCES" in drafting.SYSTEM_PROMPT
    assert "Each letter is its own" in drafting.SYSTEM_PROMPT


def test_a_re_roll_does_not_eat_a_parse_attempt(monkeypatch):
    """DRAFT_ATTEMPTS bounds parse and transport failures; the quality
    re-roll spends its own budget. Three unparseable samples, a fourth with
    a stock phrase, and the re-rolled fifth must still be accepted."""
    seen: list[str] = []
    monkeypatch.setattr(llm, "complete", _fake(
        ["garbage", "garbage", "garbage", _response(FLOSKEL), _response(CLEAN)], seen))

    body, *_ = drafting.draft_application(JOB, "profile")

    assert body == CLEAN
    assert len(seen) == 5
