"""The pure reply layer: German rule verdicts, MIME extraction, matching.

Every fixture text here is INVENTED — realistic in shape (the shapes are what
the rules exist for) but naming no real company, person or address. Real
Absagen from the owner's mailbox live in tests/fixtures/replies/private/
(gitignored); the replay test at the bottom runs them when present and is
skipped on any other machine — CI's coverage is the invented corpus.
"""

import email.message
import email.policy
import pathlib

import pytest

from jobdeck import replies

# --- invented corpus ----------------------------------------------------------

ABSAGE_KLASSISCH = """Sehr geehrter Herr Beispiel,

vielen Dank für Ihre Bewerbung und das damit verbundene Interesse an unserem
Unternehmen. Nach sorgfältiger Prüfung Ihrer Unterlagen müssen wir Ihnen
leider mitteilen, dass wir uns für einen anderen Kandidaten entschieden haben.

Für Ihren weiteren Berufsweg wünschen wir Ihnen alles Gute.

Mit freundlichen Grüßen
Personalabteilung"""

# The polite no-"leider" rejection the ROADMAP explicitly warns about.
ABSAGE_OHNE_LEIDER = """Guten Tag Herr Beispiel,

wir danken Ihnen für Ihre Bewerbung. Die Auswahl ist uns nicht leichtgefallen;
wir haben uns jedoch für Bewerberinnen und Bewerber entschieden, die dem
Anforderungsprofil der Stelle noch besser entsprochen haben.

Wir wünschen Ihnen alles Gute für Ihren weiteren Weg.

Freundliche Grüße"""

ABSAGE_ENGERE_AUSWAHL = """Sehr geehrter Herr Beispiel,

vielen Dank für Ihr Interesse. Wir müssen Ihnen heute mitteilen, dass Sie
nicht in die engere Auswahl gekommen sind. Ihre Unterlagen haben wir
selbstverständlich gelöscht.

Mit freundlichen Grüßen"""

EINLADUNG_MIT_TERMIN = """Guten Tag Herr Beispiel,

vielen Dank für Ihre aussagekräftige Bewerbung. Gerne laden wir Sie zu einem
Vorstellungsgespräch ein. Wann hätten Sie Zeit? Folgende Terminvorschläge
können wir anbieten: Dienstag 10:00 Uhr oder Mittwoch 14:30 Uhr.

Mit freundlichen Grüßen
Frau Muster, Personalreferentin"""

EINLADUNG_TELEFONAT = """Hallo Herr Beispiel,

danke für Ihre Unterlagen. Wir möchten Sie gerne in einem ersten Telefonat
kennenlernen. Passt Ihnen Donnerstag um 11 Uhr?

Beste Grüße"""

# Receipt boilerplate that NAMES the interview conditionally — the classic
# false-invitation trap the conditional screen exists for.
EINGANG_MIT_BEDINGUNG = """Sehr geehrter Herr Beispiel,

vielen Dank für Ihre Bewerbung als Softwareentwickler. Hiermit bestätigen wir
den Eingang Ihrer Unterlagen. Sollten Ihre Qualifikationen unserem Profil
entsprechen, laden wir Sie zu einem Vorstellungsgespräch ein.

Diese Nachricht wurde automatisch erstellt."""

EINGANG_SCHLICHT = """Guten Tag,

Ihre Bewerbung ist bei uns eingegangen und wird derzeit geprüft. Bitte sehen
Sie von Rückfragen zum Bearbeitungsstand ab.

Ihr Recruiting-Team"""

RUECKFRAGE = """Sehr geehrter Herr Beispiel,

vielen Dank für Ihre Unterlagen. Könnten Sie uns noch Ihr Abschlusszeugnis
nachreichen? Das würde die Prüfung beschleunigen.

Mit freundlichen Grüßen"""

# An interview being CANCELLED: invitation vocabulary + rejection act.
ABSAGE_NACH_EINLADUNG = """Sehr geehrter Herr Beispiel,

das für Freitag geplante Vorstellungsgespräch müssen wir leider absagen.
Wir haben die Stelle zwischenzeitlich intern besetzt.

Mit freundlichen Grüßen"""

OOO_MIT_DELEGATION = """Ich bin bis zum 25.08. nicht im Hause.

In dringenden Fällen zum Thema Vorstellungsgespräch wenden Sie sich bitte an
meine Kollegin Frau Muster.

Mit freundlichen Grüßen"""


# --- rule verdicts ------------------------------------------------------------

@pytest.mark.parametrize("text", [ABSAGE_KLASSISCH, ABSAGE_OHNE_LEIDER,
                                  ABSAGE_ENGERE_AUSWAHL])
def test_rejections_classify_as_absage(text):
    verdict = replies.classify("Ihre Bewerbung", text)
    assert verdict is not None and verdict.classification == "absage"


def test_the_thanking_opener_never_rescues_a_rejection():
    """Every German rejection opens by thanking for the application — the
    eingang family fires too, and absage must still win because the thanks
    is part of the rejection's own fixed form."""
    verdict = replies.classify("Ihre Bewerbung", ABSAGE_KLASSISCH)
    assert verdict.classification == "absage"


@pytest.mark.parametrize("text", [EINLADUNG_MIT_TERMIN, EINLADUNG_TELEFONAT])
def test_invitations_classify_as_einladung(text):
    verdict = replies.classify("Ihre Bewerbung", text)
    assert verdict is not None and verdict.classification == "einladung"


def test_a_receipt_naming_the_interview_conditionally_stays_a_receipt():
    verdict = replies.classify("Eingangsbestätigung", EINGANG_MIT_BEDINGUNG)
    assert verdict is not None and verdict.classification == "eingang"


def test_a_plain_receipt_classifies_as_eingang():
    verdict = replies.classify("Ihre Bewerbung", EINGANG_SCHLICHT)
    assert verdict is not None and verdict.classification == "eingang"


def test_a_question_yields_no_verdict():
    assert replies.classify("Ihre Bewerbung", RUECKFRAGE) is None


def test_a_cancelled_interview_is_ambiguous_not_einladung():
    """Invitation vocabulary + rejection act in one mail: the rules must
    refuse rather than pick — either cheap answer writes a wrong status."""
    assert replies.classify("Ihr Termin", ABSAGE_NACH_EINLADUNG) is None


def test_an_out_of_office_subject_overrides_every_family():
    verdict = replies.classify("Automatische Antwort: Ihre Bewerbung",
                               OOO_MIT_DELEGATION)
    assert verdict is not None and verdict.classification == "auto"


def test_the_verdict_carries_the_phrase_for_the_audit_note():
    verdict = replies.classify("Ihre Bewerbung", ABSAGE_ENGERE_AUSWAHL)
    assert "engere auswahl" in verdict.pattern.lower()


def test_empty_mail_yields_no_verdict():
    assert replies.classify("", "") is None


# --- machine-answer markers ---------------------------------------------------

def test_auto_submitted_headers_are_recognized():
    assert replies.is_auto_submitted({"auto-submitted": "auto-replied"})
    assert replies.is_auto_submitted({"precedence": "bulk"})
    assert replies.is_auto_submitted({"list-unsubscribe": "<mailto:x@y>"})
    assert replies.is_auto_submitted({"x-autoreply": "yes"})
    assert not replies.is_auto_submitted({"auto-submitted": "no"})
    assert not replies.is_auto_submitted({})


def test_gmail_authentication_verdict_is_read_not_computed():
    passing = {"authentication-results": "mx.google.com; spf=pass "
                                         "smtp.mailfrom=firma.example"}
    failing = {"authentication-results": "mx.google.com; spf=fail; dkim=fail"}
    assert replies.sender_authenticated(passing)
    assert not replies.sender_authenticated(failing)
    assert not replies.sender_authenticated({})


# --- MIME extraction ----------------------------------------------------------

def _mime_bytes(*, plain: str | None, html: str | None = None) -> bytes:
    message = email.message.EmailMessage(policy=email.policy.default)
    message["From"] = "HR <hr@firma.example>"
    message["Subject"] = "Ihre Bewerbung"
    if plain is not None:
        message.set_content(plain)
        if html is not None:
            message.add_alternative(html, subtype="html")
    elif html is not None:
        message.set_content(html, subtype="html")
    return message.as_bytes()


def test_extract_text_prefers_the_plain_part():
    raw = _mime_bytes(plain="Sehr geehrter Herr Beispiel, vielen Dank.",
                      html="<p>Sehr geehrter Herr Beispiel,</p>")
    assert replies.extract_text(raw).startswith("Sehr geehrter Herr Beispiel")


def test_extract_text_strips_html_only_mail():
    raw = _mime_bytes(plain=None,
                      html="<div><p>wir haben uns für einen anderen "
                           "Kandidaten entschieden.</p></div>")
    text = replies.extract_text(raw)
    assert "<" not in text
    assert "anderen Kandidaten entschieden" in text


def test_extract_text_handles_umlauts_and_quoted_printable():
    raw = _mime_bytes(plain="Wir würden Sie gerne kennenlernen — Grüße")
    assert "würden" in replies.extract_text(raw)


def test_extract_text_survives_garbage():
    """Hostile bytes must cost at most this one body, never the pass — the
    stdlib is lenient, so the contract is 'a string, no exception', not ''."""
    text = replies.extract_text(b"\xff\xfe not mime at all \x00")
    assert isinstance(text, str)


def test_extract_text_caps_the_body():
    raw = _mime_bytes(plain="x" * (replies.MAX_BODY_CHARS * 2))
    assert len(replies.extract_text(raw)) == replies.MAX_BODY_CHARS


# --- addresses and domains ----------------------------------------------------

def test_from_address_and_display_name():
    header = "Anna  Muster <Anna.Muster@Firma.example>"
    assert replies.from_address(header) == "anna.muster@firma.example"
    assert replies.from_display_name(header) == "Anna Muster"
    assert replies.from_address("") == ""


def test_matchable_domain_screens_freemail_and_subdomains():
    # a real TLD, an invented name: the PSL refuses fictional suffixes
    assert (replies.matchable_domain("hr@mail.firma-beispiel.de")
            == "firma-beispiel.de")
    assert replies.matchable_domain("recruiter@gmail.com") == ""
    assert replies.matchable_domain("hr@web.de") == ""
    assert replies.matchable_domain("") == ""


def test_refnr_matching_is_literal_and_length_screened():
    assert replies.refnr_in_text("10000-1177449Z", "Ihre Bewerbung",
                                 "Referenz 10000-1177449Z, danke.")
    assert not replies.refnr_in_text("2026", "Betreff 2026", "im Jahr 2026")
    assert not replies.refnr_in_text("", "x", "y")


# --- private fixture replay (his real Absagen; absent everywhere else) --------

_PRIVATE = pathlib.Path(__file__).parent / "fixtures" / "replies" / "private"


@pytest.mark.skipif(not any(_PRIVATE.glob("*.txt")) if _PRIVATE.is_dir()
                    else True,
                    reason="private reply fixtures not present (deliberate: "
                           "they are personal mail, gitignored; CI runs the "
                           "invented corpus above)")
def test_private_fixtures_replay():
    """Each private fixture: first line = expected classification, blank
    line, then the mail body exactly as received."""
    for path in sorted(_PRIVATE.glob("*.txt")):
        expected, _, body = path.read_text(encoding="utf-8").partition("\n\n")
        verdict = replies.classify("", body)
        got = verdict.classification if verdict is not None else "none"
        assert got == expected.strip(), path.name
