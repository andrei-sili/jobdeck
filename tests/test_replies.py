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


def test_only_dmarc_vouches_for_the_from_domain():
    """SPF authenticates the ENVELOPE and DKIM authenticates whatever domain
    signed; neither binds the From header a human reads, so anyone with a
    mailbox passes both while writing any From they like. Only DMARC
    requires an authenticated identity aligned with the From domain — which
    is the whole claim this check is used to support."""
    aligned = {"authentication-results":
               "mx.google.com; dkim=pass header.i=@firma-beispiel.de; "
               "spf=pass smtp.mailfrom=firma-beispiel.de; "
               "dmarc=pass header.from=firma-beispiel.de"}
    # the attacker's OWN domain passes SPF and DKIM for a forged From
    attacker = {"authentication-results":
                "mx.google.com; spf=pass smtp.mailfrom=angreifer.example; "
                "dkim=pass header.i=@angreifer.example; "
                "dmarc=fail header.from=firma-beispiel.de"}
    assert replies.sender_authenticated(aligned)
    assert not replies.sender_authenticated(attacker)
    assert not replies.sender_authenticated({})


def test_only_the_line_gmail_stamped_is_trusted():
    """A sender may include an Authentication-Results header of their own.
    Gmail prepends its own and the metadata reader keeps the first, but the
    check confirms the authserv-id rather than relying on that ordering."""
    forged = {"authentication-results":
              "evil.example; dmarc=pass header.from=firma-beispiel.de"}
    assert not replies.sender_authenticated(forged)


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


# --- what the review panel found: the screen must not decide alone ---------

POLITE_EINLADUNG = """Sehr geehrter Herr Beispiel,

vielen Dank für Ihre Bewerbung als Junior Python-Entwickler. Gerne würden
wir Sie zu einem Vorstellungsgespräch einladen. Bitte teilen Sie uns mit,
ob Ihnen der 25. August passt.

Mit freundlichen Grüßen"""

EINGANG_MIT_HYPOTHETISCHER_ABSAGE = """Guten Tag,

wir bestätigen den Eingang Ihrer Bewerbung. Im Falle einer negativen
Entscheidung erhalten Sie eine Absage per E-Mail.

Ihr Recruiting-Team"""

EINGANG_UND_ECHTE_ABSAGE = """Sehr geehrter Herr Beispiel,

Ihre Bewerbung ist bei uns eingegangen. Wir müssen Ihnen mitteilen, dass
wir Sie nicht weiter berücksichtigen können.

Mit freundlichen Grüßen"""


def test_a_polite_invitation_is_an_invitation_not_a_receipt():
    """German business prose uses the Konjunktiv for politeness: 'Gerne
    würden wir Sie einladen' is a real invitation. Screening it left the
    thank-you opener as the only survivor, so the canonical invitation
    filed itself as a receipt — confirmed by the review panel."""
    verdict = replies.classify("Ihre Bewerbung", POLITE_EINLADUNG)
    assert verdict is not None
    assert verdict.classification == "einladung"
    assert verdict.confident is True


def test_a_receipt_naming_a_possible_rejection_is_still_a_receipt():
    """'Im Falle einer negativen Entscheidung erhalten Sie eine Absage' is
    boilerplate. Unscreened, it hit absage, and absage-beats-eingang then
    closed a live application on the strength of its own confirmation."""
    verdict = replies.classify("Eingangsbestätigung",
                               EINGANG_MIT_HYPOTHETISCHER_ABSAGE)
    assert verdict is not None
    assert verdict.classification == "eingang"


def test_a_rejection_that_also_confirms_receipt_is_only_a_proposal():
    """Two competing families with a settled reading — but the same shape is
    produced by a receipt that merely NAMES a rejection, and the rules
    cannot tell them apart. So it is proposed, never filed."""
    verdict = replies.classify("Ihre Bewerbung", EINGANG_UND_ECHTE_ABSAGE)
    assert verdict is not None
    assert verdict.classification == "absage"
    assert verdict.confident is False


def test_the_thank_you_opener_never_competes_with_a_verdict():
    """Every German reply opens by thanking. Counting that as evidence made
    every classic rejection look like two families disagreeing."""
    verdict = replies.classify("Ihre Bewerbung", ABSAGE_KLASSISCH)
    assert verdict.classification == "absage"
    assert verdict.confident is True


def test_the_opener_alone_still_says_the_application_arrived():
    verdict = replies.classify(
        "Ihre Bewerbung",
        "Guten Tag,\n\nvielen Dank für Ihre Bewerbung. Wir melden uns.")
    assert verdict is not None
    assert verdict.classification == "eingang"


EINGANG_ZUSENDUNG = """Guten Tag Herr Beispiel,

vielen Dank für die Zusendung Ihrer Bewerbungsunterlagen für die Position
als Softwareentwickler. Wir melden uns, sobald die Sichtung abgeschlossen
ist.

Mit freundlichen Grüßen"""


def test_a_receipt_that_thanks_for_the_sending_is_still_a_receipt():
    """Two of the first five real receipts used this phrasing and none of
    the earlier patterns reached it — it names the SENDING rather than the
    arrival, so the mail read as 'no verdict'."""
    verdict = replies.classify("Ihre Bewerbung", EINGANG_ZUSENDUNG)
    assert verdict is not None
    assert verdict.classification == "eingang"


ENGLISH_RECEIPT = """Hi there,

Your application has landed. Thank you for considering a career with us!
We receive a high volume of applications and cannot always respond
individually if your application is not selected for interview. If you are
shortlisted for the next stage, we will be in touch.

Best regards"""

EINLADUNG_MIT_INTERVIEW = """Guten Tag Herr Beispiel,

wir möchten Sie gerne zu einem Interview einladen. Passt Ihnen Dienstag?

Mit freundlichen Grüßen"""


def test_an_english_receipt_is_not_read_as_a_german_invitation():
    """These rules judge German. A bare \\binterview\\b matched English mail
    they were never meant to read: a receipt saying 'our team will be in
    touch to arrange interviews' was proposed as an invitation on the first
    real read of his mailbox."""
    verdict = replies.classify("Back-End Engineer", ENGLISH_RECEIPT)
    assert verdict is None or verdict.classification != "einladung"


def test_a_german_invitation_saying_interview_still_counts():
    """The narrowing must not cost the word where German grammar puts it."""
    verdict = replies.classify("Ihre Bewerbung", EINLADUNG_MIT_INTERVIEW)
    assert verdict is not None
    assert verdict.classification == "einladung"


FORMELLER_BRIEF = """Behörde für Beispiele | Postfach 13 20 | 54203 Trier

Herr Max Beispiel
Musterweg 1
12345 Musterstadt

Telefon 0651 9494-0
Telefax 0651 9494-170
poststelle@behoerde.example

Mein Aktenzeichen 03 041/12
Bitte immer angeben!

Sehr geehrter Herr Beispiel,

ich möchte Sie zu einem Vorstellungsgespräch am Dienstag, 25. August 2026,
um 11:30 Uhr in Raum 201 einladen.

Mit freundlichen Grüßen"""


def test_the_letterhead_is_not_the_letter():
    """A formal German reply puts hundreds of characters of letterhead in
    front of the sentence that matters — the excerpt of a real invitation
    showed a postbox and a fax number where the room and the time were."""
    body = replies.letter_body(FORMELLER_BRIEF)
    assert body.startswith("Sehr geehrter Herr Beispiel")
    assert "Vorstellungsgespräch" in body[:200]
    assert "Postfach" not in body


def test_a_letter_without_a_salutation_is_left_whole():
    text = "Ihre Bewerbung ist eingegangen."
    assert replies.letter_body(text) == text
    assert replies.letter_body("") == ""
