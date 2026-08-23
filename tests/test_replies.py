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

# A polite rejection without the common "leider" marker.
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


@pytest.mark.parametrize("gap, name", [
    ("\u00a0", "NO-BREAK SPACE — Word and Outlook emit it around German "
             "punctuation"),
    ("  ", "double space — what HTML-to-text conversion leaves behind"),
    ("\t", "tab"),
    ("\u202f", "NARROW NO-BREAK SPACE"),
    ("\r\n", "CRLF, which is what actually arrives on the wire"),
])
def test_an_exotic_space_between_two_words_still_matches(gap, name):
    """Two thirds of the patterns contain a literal U+0020, so whatever a
    mail client puts between the words instead used to delete the verdict
    outright — the mail read as unclassifiable rather than as a rejection.
    Every real message in the owner's mailbox carried one of these."""
    verdict = replies.classify(
        "Ihre Bewerbung",
        f"Wir müssen Ihnen leider{gap}mitteilen, dass es nicht geklappt hat.")
    assert verdict is not None, f"{name} killed the verdict"
    assert verdict.classification == "absage"


def test_a_soft_hyphen_inside_a_compound_still_matches():
    """Justified HTML mail hyphenates long German compounds, and the
    invitation family is built on exactly such a compound."""
    verdict = replies.classify(
        "Termin",
        # the compound is the ONLY invitation evidence here: with
        # "laden Sie ... ein" in the sentence the verdict arrives by a
        # second route and the test cannot see the hyphen at all.
        "Wir freuen uns auf das Vorstellungs\u00adgespräch am Dienstag.")
    assert verdict is not None and verdict.classification == "einladung"


def test_normalising_whitespace_does_not_defeat_the_conditional_screen():
    """The screen is what stops a receipt naming a possible interview from
    reading as one — normalisation must not flatten the paragraph structure
    it depends on."""
    verdict = replies.classify(
        "Eingangsbestätigung",
        "Vielen Dank für Ihre Bewerbung.\n\nSollten Ihre Unterlagen unserem "
        "Profil entsprechen, laden wir Sie zu\u00a0einem Vorstellungsgespräch "
        "ein.")
    assert verdict is not None and verdict.classification == "eingang"


@pytest.mark.parametrize("body", [
    "Nach Sichtung Ihrer Unterlagen können wir Sie leider nicht zu einem "
    "persönlichen Gespräch einladen.",
    "Eine Einladung zu einem Vorstellungsgespräch können wir Ihnen leider "
    "nicht aussprechen.",
    "Leider können wir Sie nicht einladen.",
])
def test_a_rejection_dressed_as_an_invitation_never_files_one(body):
    """German builds the rejection out of the invitation's own words, and no
    amount of invitation phrasing tells them apart. Filed confidently this
    was the most expensive misfile the rules can produce: Einladung is rank
    4, so it closes the application AND then, by the anti-downgrade rule,
    refuses the true Absage arriving behind it.

    The invariant is what matters, not which mechanism delivers it — naming
    the phrase in the Absage family makes the mail two-family, and the
    negation screen catches what no phrase names."""
    verdict = replies.classify("Ihre Bewerbung", body)
    assert verdict is None or not (verdict.classification == "einladung"
                                   and verdict.confident)


def test_the_negation_screen_alone_demotes_an_unnamed_refusal():
    """The screen has to stand on its own: this refusal matches no Absage
    phrase, so nothing else can stop it filing as a confident invitation."""
    verdict = replies.classify("Ihre Bewerbung",
                               "Leider können wir Sie nicht einladen.")
    assert verdict is not None and verdict.classification == "einladung"
    assert not verdict.confident


def test_a_negation_in_another_sentence_leaves_an_invitation_confident():
    """The screen is per SENTENCE. A real invitation routinely says "nicht"
    about something else — where the interview is, what not to bring — and
    demoting it there would cost a click on every genuine invitation."""
    verdict = replies.classify(
        "Termin",
        "Wir laden Sie zu einem Vorstellungsgespräch ein. Das Gespräch findet "
        "nicht in unserer Zentrale statt.")
    assert verdict.classification == "einladung" and verdict.confident


def test_the_polite_opener_alone_never_writes_a_status():
    """Every German reply opens by thanking for the application — a rejection
    included — so the opener is the weakest evidence in the module. Carrying
    a CONFIDENT verdict, it filed every unrecognised rejection as "your
    application arrived": status In Bearbeitung, the Offen label, and no
    review row, so nothing ever asked about it again."""
    verdict = replies.classify(
        "Ihre Bewerbung",
        "Vielen Dank für Ihre Bewerbung. Wir melden uns.")
    assert verdict is not None and verdict.classification == "eingang"
    assert not verdict.confident
    # …while a receipt that states the arrival keeps writing by itself.
    real = replies.classify("Eingang", EINGANG_SCHLICHT)
    assert real.classification == "eingang" and real.confident


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


# --- the false-Absage boundary ------------------------------------------------
#
# A wrong Absage is the expensive error in this system: it is rank 4, so it
# CLOSES a live application, and it is silent — the row leaves the open
# statuses so no follow-up fires, and the mail is filed rather than left
# waiting. So the rejection family is allowed to grow only against a corpus
# of mails that must never be one.

NEAR_MISSES = [
    ("Einladung", "Gerne laden wir Sie zu einem Vorstellungsgespräch ein. "
                  "Passt Ihnen Dienstag um 11:30 Uhr?"),
    ("Terminwahl", "Wir würden Sie gerne kennenlernen. Wann hätten Sie Zeit?"),
    ("Ihr Termin", "Das Gespräch findet nicht in unserer Zentrale statt, "
                   "sondern in der Niederlassung."),
    ("Terminverschiebung", "Wir müssen den Termin am Freitag leider "
                           "verschieben. Passt Ihnen Montag?"),
    ("Eingangsbestätigung", "Ihre Bewerbung ist bei uns eingegangen. "
                            "Die Stelle soll zum 01.10. besetzt werden."),
    ("Eingang", "Wir bestätigen den Erhalt Ihrer Unterlagen. Die Stelle ist "
                "zum nächsten Quartal neu zu besetzen."),
    ("Ihre Bewerbung", "Vielen Dank für Ihre Bewerbung. Wir sprechen derzeit "
                       "noch mit anderen Bewerbern und melden uns."),
    ("Ihre Bewerbung", "Falls wir uns für einen anderen Bewerber entscheiden, "
                       "erhalten Sie eine Nachricht."),
    ("Ihre Bewerbung", "Sollten Sie nicht in die engere Auswahl kommen, "
                       "melden wir uns dennoch."),
    ("Rückfrage", "Könnten Sie uns noch Ihr Abschlusszeugnis nachreichen?"),
    ("Unterlagen", "Bitte senden Sie uns den unterschriebenen "
                   "Datenschutzhinweis zurück."),
    ("Gehalt", "Ihre Gehaltsvorstellung haben wir notiert und besprechen sie "
               "im Gespräch."),
    ("Profil", "Ihr Profil entspricht genau dem Anforderungsprofil."),
    ("Zusage", "Wir freuen uns, Ihnen die Stelle anbieten zu können. Die "
               "Entscheidung ist auf Sie gefallen."),
    ("Information", "Leider müssen wir unsere Karrieremesse am 12.09. "
                    "absagen."),
    ("Newsletter", "Neue Stellen in Ihrer Region. Jetzt bewerben!"),
    ("Abwesenheit", "Ich bin bis zum 25.08. nicht im Hause."),
    ("Vorstellung", "Die Stelle ist noch nicht besetzt und wir würden Sie "
                    "unserem Kunden gerne vorstellen."),
    ("Bewerberpool", "Damit Ihre Bewerbung im Auswahlverfahren berücksichtigt "
                     "werden kann, fehlt uns noch Ihr Lebenslauf."),
    ("Ihr Termin", "Sollte das Vorstellungsgespräch nicht stattfinden können, "
                   "melden wir uns rechtzeitig."),
]


@pytest.mark.parametrize("subject, body", NEAR_MISSES)
def test_no_near_miss_is_ever_read_as_a_rejection(subject, body):
    verdict = replies.classify(subject, body)
    assert verdict is None or verdict.classification != "absage", (
        f"false Absage on {subject!r}: {verdict}")


@pytest.mark.parametrize("subject, body", NEAR_MISSES)
def test_no_near_miss_writes_a_status_it_has_not_earned(subject, body):
    """Beyond the family: a near-miss may be read as something, but only a
    receipt or a real invitation may do so CONFIDENTLY."""
    verdict = replies.classify(subject, body)
    if verdict is not None and verdict.confident:
        assert verdict.classification in ("eingang", "einladung", "auto"), (
            f"{subject!r} confidently wrote {verdict.classification}")


# --- the families the audit found entirely absent -----------------------------

NEUE_ABSAGEN = [
    ("die Stelle ist weg",
     "Die von Ihnen angefragte Stelle wurde zwischenzeitlich anderweitig "
     "besetzt."),
    ("intern besetzt",
     "Die Position wurde inzwischen intern besetzt."),
    ("Vakanz zurückgezogen",
     "Unser Kunde hat das Projekt gestoppt und die Vakanz zurückgezogen."),
    ("Einstellungsstopp",
     "Wir stellen zurzeit niemanden ein."),
    ("nicht erfolgreich",
     "Ihre Bewerbung war im Auswahlverfahren leider nicht erfolgreich."),
    ("nicht überzeugt",
     "Ihre Bewerbung hat uns leider nicht überzeugt."),
    ("nicht ausgewählt",
     "Sie wurden für diese Position leider nicht ausgewählt."),
    ("nächste Runde",
     "Ihre Bewerbung wurde nicht für die nächste Auswahlrunde ausgewählt."),
    ("negativer Bescheid",
     "Wir müssen Ihnen leider einen negativen Bescheid erteilen."),
    ("nicht positiv bescheiden",
     "Wir konnten Ihre Bewerbung leider nicht positiv bescheiden."),
    ("keine positive Rückmeldung",
     "Wir bedauern, Ihnen keine positive Rückmeldung geben zu können."),
    ("abgelehnt im Betreff",
     "Ihre Bewerbung wurde abgelehnt."),
    ("anderweitig entschieden",
     "Wir haben uns anderweitig entschieden."),
    ("gegen die Bewerbung",
     "Wir haben uns schweren Herzens gegen Ihre Bewerbung entschieden."),
    ("jemand anderes",
     "Wir haben uns für jemand anderen entschieden."),
    ("interner Kandidat",
     "Unser Mandant hat sich für einen internen Kandidaten entschieden."),
    ("zugunsten eines anderen",
     "Die Entscheidung ist zugunsten eines anderen Bewerbers gefallen."),
    ("Wahl auf eine Mitbewerberin",
     "Die Entscheidung ist am Ende auf eine Mitbewerberin gefallen."),
    ("Verfahren fortsetzen",
     "Wir setzen das Verfahren mit anderen Kandidaten fort."),
    ("separables Verb",
     "Wir sagen Ihnen daher hiermit ab."),
    ("Berücksichtigung nicht möglich",
     "Eine Berücksichtigung im laufenden Verfahren ist uns leider nicht "
     "möglich."),
    ("nicht weiter zu verfolgen",
     "Wir haben uns entschieden, Ihre Bewerbung nicht weiter zu verfolgen."),
    ("Einstellung kommt nicht in Betracht",
     "Eine Einstellung kommt daher leider nicht in Betracht."),
    ("nicht zu Ihren Gunsten",
     "Die Auswahl ist leider nicht zu Ihren Gunsten ausgefallen."),
    ("Anforderungsprofil, andere Wortstellung",
     "Ihre Qualifikationen entsprechen nicht dem von uns gesuchten "
     "Anforderungsprofil."),
    ("Profil passt nicht",
     "Leider passt Ihr Profil nicht zu den Anforderungen dieser Position."),
    ("es hat nicht geklappt",
     "Es hat diesmal leider nicht geklappt."),
    ("keine Zusage",
     "Wir können Ihnen leider keine Zusage geben."),
    ("nicht auf Sie zurück",
     "Für die ausgeschriebene Position kommen wir leider nicht auf Sie "
     "zurück."),
    ("dem Kunden nicht vorstellen",
     "Wir konnten Sie unserem Mandanten leider nicht vorstellen."),
    ("Unterlagen zurück",
     "Anbei senden wir Ihnen Ihre Bewerbungsunterlagen zurück."),
    ("alles Gute für Sie",
     "Ihre Bewerbung war nicht erfolgreich. Trotzdem alles Gute für Sie."),
]


@pytest.mark.parametrize("name, body", NEUE_ABSAGEN)
def test_the_missing_rejection_families_are_read_as_rejections(name, body):
    """One case per family the audit found the rules could not see at all.
    Without these the mail fell through to the polite opener and filed itself
    as a receipt — the mechanism that put a mailbox of rejections under
    JobDeck/Offen."""
    verdict = replies.classify("Ihre Bewerbung", body)
    assert verdict is not None, f"{name}: no verdict"
    assert verdict.classification == "absage", f"{name}: {verdict}"


def test_a_rejection_that_also_mentions_an_event_stays_a_rejection():
    """The event screen must engage only where the application is never
    named — an employer can perfectly well cancel a workshop and reject an
    application in the same mail."""
    verdict = replies.classify(
        "Ihre Bewerbung",
        "Unseren Workshop am Freitag müssen wir leider absagen. Zu Ihrer "
        "Bewerbung: die Stelle wurde inzwischen intern besetzt.")
    assert verdict is not None and verdict.classification == "absage"


# --- the company-name arm: the only one a form application can be reached by --

@pytest.mark.parametrize("firma, sender, expected", [
    ("Firma Beispiel GmbH", "hr@firma-beispiel.de", True),
    ("Müller & Co. KG", "bewerbung@mueller-co.de", True),
    ("Beispiel Holding AG", "no-reply@beispiel.com", True),
    # the ATS writes from its own domain and names the employer in the
    # display part — the shape a portal application actually replies in
    ("Firma Beispiel GmbH", "no-reply@ats-anbieter.com", False),
    # Freemail says nothing about who the sender is — and the guard has to
    # be real, not incidental: a company name that RESEMBLES the freemail
    # host is what tells the two apart. Anyone can hold outlook.com mail.
    ("Firma Beispiel GmbH", "chef@gmx.de", False),
    ("Outlook Systeme GmbH", "chef@outlook.com", False),
    ("Web Solutions GmbH", "kontakt@web.de", False),
    # a different employer must never collide
    ("Firma Beispiel GmbH", "hr@ganz-anders.de", False),
    # too short to compare: "IT GmbH" would otherwise reach italia.de
    ("IT GmbH", "kontakt@italia.de", False),
])
def test_the_company_name_arm_recognises_only_its_own_sender(
        firma, sender, expected):
    assert replies.company_in_sender(firma, f"HR <{sender}>", sender) is expected


def test_the_display_name_carries_the_employer_when_the_domain_cannot():
    """An ATS sends from ats-anbieter.com and puts the employer in front of
    the angle bracket. Without this, every portal application's reply is
    unreachable."""
    assert replies.company_in_sender(
        "Firma Beispiel GmbH",
        "Personalabteilung Firma Beispiel GmbH <no-reply@ats-anbieter.com>",
        "no-reply@ats-anbieter.com")


def test_the_company_key_survives_the_spellings_a_domain_forces():
    """A domain has no umlauts and no ampersands, so the key has to meet it
    where a German registrar puts it."""
    assert replies.company_key("Müller & Co. KG") == "mueller"
    assert replies.company_key("Groß Software GmbH") == "grosssoftware"
    assert replies.company_key("Beispiel Holding AG") == "beispiel"


# --- his own words are not the employer's verdict -----------------------------

REPLY_WITH_QUOTE = """Sehr geehrter Herr Beispiel,

vielen Dank für Ihr Interesse. Wir haben Ihr Profil bewertet und sind leider
zu dem Ergebnis gekommen, dass wir Ihnen im Moment keine Ihren Kenntnissen
und Fähigkeiten entsprechende Position anbieten können.

Mit freundlichen Grüßen

-----Ursprüngliche Nachricht-----
Von: Andrei <andrei@example.org>
Gesendet: Montag, 9. Juni 2026 14:02
An: karriere@firma-beispiel.de
Betreff: Bewerbung als Softwareentwickler

Sehr geehrte Damen und Herren,

meine Bewerbungsunterlagen sende ich Ihnen im Anhang. Über ein kurzes
Gespräch würde ich mich sehr freuen.

Mit freundlichen Grüßen
Andrei"""


def test_the_quoted_application_is_not_read_as_the_employers_answer():
    """Quoted sent text must not be classified as the employer's answer.

    Without quote handling, wording from the sent application can outweigh the
    actual response and produce an incorrect invitation classification.
    """
    verdict = replies.classify("AW: Bewerbung als Softwareentwickler",
                               REPLY_WITH_QUOTE)
    assert verdict is not None and verdict.classification == "absage"


@pytest.mark.parametrize("marker", [
    "-----Ursprüngliche Nachricht-----",
    "-----Original Message-----",
    "--------- Weitergeleitete Nachricht ---------",
    "________________________________",
    "Von: Andrei <andrei@example.org>",
    "Am 09.06.2026 um 14:02 schrieb Andrei:",
    "On 9 Jun 2026, at 14:02, Andrei wrote:",
    "> meine Bewerbungsunterlagen sende ich Ihnen im Anhang",
])
def test_every_quote_marker_ends_the_senders_turn(marker):
    body = (f"Guten Tag,\n\nvielen Dank.\n\n{marker}\n"
            "Über ein kurzes Gespräch würde ich mich sehr freuen.")
    assert "Gespräch" not in replies.strip_quoted(body), marker


def test_a_bare_forward_yields_nothing_rather_than_a_guess():
    """When the sender wrote nothing of their own, no verdict is the honest
    answer — the review pile exists for exactly this."""
    body = ("-----Weitergeleitete Nachricht-----\n"
            "Über ein kurzes Gespräch würde ich mich sehr freuen.")
    assert replies.strip_quoted(body) == ""


def test_the_employers_own_text_survives_the_strip():
    """The cut must not eat the answer: everything above the first marker is
    what the sender wrote this time."""
    kept = replies.strip_quoted(
        "Sehr geehrte Frau Muster,\n\nwir laden Sie ein.\n\n"
        "Am 09.06.2026 um 14:02 schrieb Andrei:\n> Hallo")
    assert "wir laden Sie ein." in kept and "Hallo" not in kept
