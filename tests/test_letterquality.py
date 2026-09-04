"""What a letter says about itself, measured without a model.

The parser gate ranks on the advert's own terms; the human gate spots the
machine by its stock phrases, its copied sentences and its reused opening.
Both are computed from text, so both are pinned here exactly.
"""

from jobdeck.ai import letterquality as lq

POSTING = """Wir suchen einen Backend Developer (m/w/d). Sie entwickeln REST-APIs
mit Python und Django REST Framework, arbeiten mit PostgreSQL und Docker und
bringen Erfahrung mit CI/CD und Code Reviews mit. Englisch in Wort und Schrift.
Wir bieten ein agiles Team, Homeoffice und ein modernes Büro in Köln."""

LETTER = """Sehr geehrte Damen und Herren,

bei Beispiel GmbH habe ich REST-Endpunkte mit Python und DRF umgesetzt und
die Änderungen in Code Reviews mit dem Team abgestimmt.

Docker und PostgreSQL setze ich in meinem eigenen Projekt ein.

Ich bin ab sofort verfügbar."""


def test_terms_are_found_in_the_adverts_spelling_and_the_letters_variants():
    assert lq.terms_in(POSTING) == [
        "Backend", "REST", "Python", "Django REST Framework", "PostgreSQL",
        "Docker", "CI/CD", "Code Review", "Englisch", "Agile"]
    # "REST-APIs" is one term, not REST and API; "Django REST Framework" is
    # one term, not Django and Django REST Framework
    assert "API" not in lq.terms_in(POSTING)
    assert "Django" not in lq.terms_in(POSTING)
    # "DRF" in the letter counts as the advert's "Django REST Framework"
    assert "Django REST Framework" in lq.terms_in(LETTER)
    # a bare word inside another word is not a term
    assert "Git" not in lq.terms_in("Digital ist kein Werkzeug.")
    assert "REST" not in lq.terms_in("Der Rest bleibt.")  # the noun, not the API
    assert "REST" in lq.terms_in("REST-Schnittstellen")


def test_coverage_counts_letter_and_cv_separately_and_names_what_neither_has():
    cov = lq.coverage(POSTING, LETTER, cv_text="Python · Docker · CI/CD (GitHub Actions)")

    assert cov.terms == ("Backend", "REST", "Python", "Django REST Framework",
                         "PostgreSQL", "Docker", "CI/CD", "Code Review",
                         "Englisch", "Agile")
    assert cov.in_letter == ("REST", "Python", "Django REST Framework",
                             "PostgreSQL", "Docker", "Code Review")
    assert cov.in_cv == ("Python", "Docker", "CI/CD")
    assert cov.missing == ("Backend", "Englisch", "Agile")
    assert cov.line() == ("Begriffe aus der Anzeige: 6 von 10 im Brief · 3 im "
                          "Lebenslauf · in keinem: Backend, Englisch, Agile")


def test_an_advert_naming_no_known_term_gets_no_line():
    assert lq.coverage("Wir suchen Sie.", LETTER).line() == ""


def test_the_stock_phrases_recruiters_name_are_found_in_order():
    text = ("Mit großem Interesse habe ich Ihre Anzeige gelesen. Die Aufgabe "
            "reizt mich besonders, und ich bin überzeugt, dass ich passe.")
    assert lq.floskeln(text) == ["mit großem interesse habe ich",
                                 "reizt mich besonders",
                                 "ich bin überzeugt, dass ich"]
    assert lq.floskeln("Bei Beispiel GmbH habe ich Endpunkte umgesetzt.") == []


def test_a_sentence_lifted_from_the_advert_is_found_despite_punctuation():
    letter = ("Sehr geehrte Damen und Herren,\n\nSie entwickeln REST-APIs mit "
              "Python und Django REST Framework; arbeiten mit PostgreSQL.")
    spans = lq.copied_spans(letter, POSTING)
    assert spans == ["sie entwickeln rest apis mit python und django rest "
                     "framework arbeiten mit postgresql"]
    # mirroring the TERMS is not copying
    assert lq.copied_spans(LETTER, POSTING) == []


def test_the_opening_is_the_first_words_after_the_anrede():
    assert lq.opening(LETTER) == "bei beispiel gmbh habe ich rest"
    earlier = "Sehr geehrte Frau Weber,\n\nBei Beispiel GmbH habe ich REST-APIs gebaut."
    assert lq.repeats_an_opening(LETTER, [earlier])
    assert not lq.repeats_an_opening(LETTER, ["Guten Tag,\n\nMein Weg in die IT begann 2020."])
    assert not lq.repeats_an_opening("", [earlier])


def test_notes_say_each_thing_once_in_german():
    letter = ("Sehr geehrte Damen und Herren,\n\nSie entwickeln REST-APIs mit "
              "Python und Django REST Framework, arbeiten mit PostgreSQL und "
              "Docker. Die Aufgabe reizt mich besonders.")
    found = lq.notes(letter, POSTING, previous=[letter])
    assert [n.kind for n in found] == ["floskel", "kopie", "einstieg"]
    assert found[0].text == "Floskel: „reizt mich besonders“"
    assert found[1].text.startswith("Wörtlich aus der Anzeige: „sie entwickeln")
    assert found[2].text == "Beginnt wie ein früherer Brief"
    assert lq.notes(LETTER, POSTING) == []


def test_the_retry_hint_names_what_to_avoid_and_nothing_else():
    hint = lq.retry_hint(lq.notes(
        "Sehr geehrte Damen und Herren,\n\nDie Aufgabe reizt mich besonders.",
        POSTING))
    assert hint.startswith("\n\nNote: your previous draft contained stock phrases")
    assert "reizt mich besonders" in hint
    assert "copied" not in hint and "opening" not in hint


def test_cv_text_strips_markup_and_style_and_tolerates_a_missing_file(tmp_path):
    page = tmp_path / "cv.html"
    page.write_text("<style>body{color:red}</style><h1>Erika &amp; Co</h1>"
                    "<p>Python, <b>Docker</b></p>", encoding="utf-8")
    text = lq.text_of_html(page)
    assert "Erika & Co" in text and "Docker" in text and "color" not in text
    assert lq.text_of_html(tmp_path / "nein.html") == ""
    assert lq.text_of_html(None) == ""
