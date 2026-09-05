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
        "Docker", "CI/CD", "Code Review", "Englisch", "Agil"]
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
                         "Englisch", "Agil")
    assert cov.in_letter == ("REST", "Python", "Django REST Framework",
                             "PostgreSQL", "Docker", "Code Review")
    assert cov.in_cv == ("Python", "Docker", "CI/CD")
    assert cov.missing == ("Backend", "Englisch", "Agil")
    assert cov.line() == ("Begriffe aus der Anzeige: 6 von 10 im Brief · 3 im "
                          "Lebenslauf · weder im Brief noch im Lebenslauf: "
                          "Backend, Englisch, Agil")


def test_an_advert_naming_no_known_term_gets_no_line():
    assert lq.coverage("Wir suchen Sie.", LETTER).line() == ""


def test_the_stock_phrases_recruiters_name_are_found_in_order_and_quoted_as_written():
    text = ("Mit großem Interesse habe ich Ihre Anzeige gelesen. Die Aufgabe "
            "reizt mich besonders, und ich bin überzeugt, dass ich passe.")
    assert lq.floskeln(text) == ["Mit großem Interesse habe ich",
                                 "reizt mich besonders",
                                 "ich bin überzeugt, dass ich"]
    assert lq.floskeln("Bei Beispiel GmbH habe ich Endpunkte umgesetzt.") == []


def test_a_phrase_broken_by_a_line_wrap_is_still_found():
    assert lq.floskeln("Die Aufgabe reizt\nmich besonders.") == ["reizt mich besonders"]


def test_the_subjunctive_close_is_found_in_every_shape():
    for close in ("Über ein Gespräch würde ich mich freuen.",
                  "Ich würde mich über eine Einladung freuen.",
                  "Über eine Einladung zum Vorstellungsgespräch würde ich mich "
                  "sehr freuen."):
        assert lq.floskeln(close), close
    assert lq.floskeln("Ich freue mich auf Ihre Rückmeldung.") == []


def test_the_adjective_list_is_a_floskel_whatever_its_order():
    assert lq.floskeln("Ich bin flexibel und belastbar.") == ["flexibel und belastbar"]
    assert lq.floskeln("Ich bin zuverlässig, motiviert und engagiert.") == [
        "zuverlässig, motiviert und engagiert"]
    # one adjective with its example is a claim, not a list
    assert lq.floskeln("Belastbar war ich im Schichtdienst als Disponent.") == []


def test_a_sentence_lifted_from_the_advert_is_found_despite_punctuation():
    letter = ("Sehr geehrte Damen und Herren,\n\nSie entwickeln REST-APIs mit "
              "Python und Django REST Framework; arbeiten mit PostgreSQL.")
    spans = lq.copied_spans(letter, POSTING)
    assert spans == ["Sie entwickeln REST-APIs mit Python und Django REST "
                     "Framework; arbeiten mit PostgreSQL"]
    # mirroring the TERMS is not copying
    assert lq.copied_spans(LETTER, POSTING) == []


def test_the_job_title_may_be_repeated_the_way_the_advert_writes_it():
    """"(m/w/d)" alone is three words; a letter naming the position as the
    advert does must not burn the one re-roll on it."""
    title = "Junior Softwareentwickler Python Django (m/w/d) in Vollzeit"
    posting = f"Wir suchen: {title}. Python und Docker."
    letter = f"Sehr geehrte Damen und Herren,\n\nals {title} bringe ich Python mit."
    assert lq.copied_spans(letter, posting) != []
    assert lq.copied_spans(letter, posting, allowed=title) == []


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
    assert found[1].text.startswith("Wörtlich aus der Anzeige: „Sie entwickeln")
    # the opening is quoted, so the re-roll knows what to avoid
    assert found[2].text == ("Beginnt wie ein früherer Brief: „Sie entwickeln "
                             "REST-APIs mit Python“")
    assert lq.notes(LETTER, POSTING) == []
    two = lq.notes("Sehr geehrte Damen und Herren,\n\nMit großem Interesse "
                   "habe ich gelesen. Das reizt mich besonders.", POSTING)
    assert two[0].text.startswith("Floskeln: „")


def test_the_retry_hint_names_what_to_avoid_and_nothing_else():
    hint = lq.retry_hint(lq.notes(
        "Sehr geehrte Damen und Herren,\n\nDie Aufgabe reizt mich besonders.",
        POSTING))
    assert hint.startswith("\n\nNote: your previous draft contained stock phrases")
    assert "reizt mich besonders" in hint
    assert "copied" not in hint and "opening" not in hint
    # the opening re-roll names the opening to avoid — a hint without it
    # cannot change what the next sample opens with
    again = lq.retry_hint(lq.notes(LETTER, POSTING, previous=[LETTER]))
    assert "„bei Beispiel GmbH habe ich REST“" in again
    assert "different one of the three angles" in again


def test_cv_words_strip_markup_and_style_and_tolerate_a_missing_file(tmp_path):
    page = tmp_path / "cv.html"
    page.write_text("<style>body{color:red}</style><h1>Erika &amp; Co</h1>"
                    "<p>Python, <b>Docker</b></p>", encoding="utf-8")
    text = lq.cv_words(page).text
    assert "Erika & Co" in text and "Docker" in text and "color" not in text
    assert lq.cv_words(tmp_path / "nein.html") == ("", "")
    assert lq.cv_words(None) == ("", "")


def test_without_a_cv_the_line_counts_the_letter_alone_and_says_so():
    cov = lq.coverage(POSTING, LETTER, cv_text="")
    assert not cov.cv_known
    assert cov.line() == ("Begriffe aus der Anzeige: 6 von 10 im Brief · nicht im "
                          "Brief: Backend, CI/CD, Englisch, Agil · Lebenslauf nicht lesbar")


def test_a_term_list_in_the_adverts_order_is_mirroring_not_copying():
    """The prompt asks for the advert's TERMS; a list of them in the
    advert's order must not burn the paid re-roll as a copied sentence."""
    posting = "Kenntnisse in Python, Django REST Framework, PostgreSQL, Docker und CI/CD."
    letter = ("Sehr geehrte Damen und Herren,\n\nmeine Werkzeuge: Python, Django "
              "REST Framework, PostgreSQL, Docker und CI/CD, täglich im Einsatz.")
    assert lq.copied_spans(letter, posting) == []


def test_ordinary_german_is_not_a_floskel():
    for text in ("Erfahrung mit Docker bringe ich mit.",
                 "Die Werkzeuge, die Sie suchen, kenne ich aus dem Projekt."):
        assert lq.floskeln(text) == [], text
    assert lq.floskeln("Sie suchen einen Entwickler mit Django-Erfahrung.") \
        == ["Sie suchen einen"]


def test_the_title_rule_needs_the_whole_span_inside_the_title():
    title = "Junior Softwareentwickler Python Django (m/w/d) in Vollzeit"
    posting = (f"Wir suchen: {title}. Dazu Kundensupport und Mentoring im Team "
               "gehören zu den Aufgaben.")
    letter = ("Sehr geehrte Damen und Herren,\n\nDazu Kundensupport und Mentoring im "
              "Team gehören zu den Aufgaben, sagt Ihre Anzeige.")
    # the copied sentence merely SHARES words with the title — it is not it
    assert lq.copied_spans(letter, posting, allowed=title) != []


# -- the profile line is held to the same reader --------------------------------
def test_the_profile_line_is_checked_for_phrases_copies_and_length():
    profil = ("Hochmotivierter Fachinformatiker. Sie entwickeln REST-APIs mit "
              "Python und Django REST Framework, arbeiten mit PostgreSQL und Docker.")
    found = lq.notes(LETTER, POSTING, profil=profil)

    assert [n.kind for n in found] == ["floskel", "kopie"]
    assert found[0].text == "Floskel im Profil: „Hochmotiviert“"
    assert found[1].text.startswith("Wörtlich aus der Anzeige im Profil: „Sie entwickeln")
    hint = lq.retry_hint(found)
    assert "in the profile line: „Hochmotiviert“" in hint
    assert "copied from the advert in the profile line: „Sie entwickeln" in hint

    too_long = ("Entwickler. " + "Python bei Beispiel GmbH, " * 14).strip()
    assert len(too_long) > lq.PROFIL_MAX_CHARS
    found = lq.notes(LETTER, POSTING, profil=too_long)
    assert [n.kind for n in found] == ["profil_lang"]
    assert found[0].text.startswith(f"Profilzeile zu lang: {len(too_long)} Zeichen")
    assert f"longer than {lq.PROFIL_MAX_CHARS} characters" in lq.retry_hint(found)


def test_a_clean_profile_line_adds_no_note():
    assert lq.notes(LETTER, POSTING, profil="Fachinformatiker. Python bei "
                                             "Beispiel GmbH, ab sofort.") == []
    assert lq.notes(LETTER, POSTING) == []  # no profile line at all


def test_coverage_names_the_profile_line_and_counts_it_into_the_cv():
    """The profile line is printed into the CV, so its terms are CV terms —
    and it is named on its own, because it is the field a parser weighs as
    the summary and the draft wrote it for this advert."""
    cov = lq.coverage(POSTING, LETTER, cv_text="Python · Docker",
                      profil="Backend-Entwickler. CI/CD und Python bei Beispiel GmbH.")

    assert cov.in_profil == ("Backend", "Python", "CI/CD")
    assert cov.in_cv == ("Backend", "Python", "Docker", "CI/CD")
    assert cov.missing == ("Englisch", "Agil")
    assert cov.line() == ("Begriffe aus der Anzeige: 6 von 10 im Brief · 3 im Profil "
                          "· 4 im Lebenslauf · weder im Brief noch im Lebenslauf: "
                          "Englisch, Agil")
    # no CV file, but a profile line: it is still named and still counted
    cov = lq.coverage(POSTING, LETTER, profil="Backend-Entwickler mit CI/CD.")
    assert cov.line() == ("Begriffe aus der Anzeige: 6 von 10 im Brief · 2 im Profil "
                          "· nicht im Brief: Englisch, Agil · Lebenslauf nicht lesbar")
    # None is "the draft wrote none": nothing named, the line reads as before
    assert lq.coverage(POSTING, LETTER, cv_text="Python", profil=None).line() \
        == lq.coverage(POSTING, LETTER, cv_text="Python").line()


def test_cv_words_hold_the_profile_region_apart(tmp_path):
    path = tmp_path / "cv.html"
    path.write_text("<style>p{}</style><p>Python &amp; Docker</p>"
                    "<p><!--PROFIL-->Feste Zeile mit Git.<!--/PROFIL--></p>",
                    encoding="utf-8")
    words = lq.cv_words(path)
    assert "Docker" in words.text and "Git" not in words.text and "p{}" not in words.text
    assert words.profil == "Feste Zeile mit Git."
    assert lq.cv_words(tmp_path / "fehlt.html") == ("", "")
    path.write_bytes(b"\xff\xfe not utf-8")
    assert lq.cv_words(path) == ("", "")
