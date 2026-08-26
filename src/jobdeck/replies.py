"""What an inbound e-mail says, decided by rules before anyone pays a model.

Pure functions only: MIME in, verdicts out. The German patterns here are the
whole point of the module — headers cannot classify German HR mail (the
canonical rejection and invitation carry the same neutral subject and both
open by thanking the applicant), so the BODY is read, locally, and a verdict
is returned only when exactly one family of phrases matched outside a
conditional sentence. Everything else is honestly "no verdict": the review
pile and the (separately gated) model exist for precisely that remainder.

The conditional screen carries more weight than it looks: German receipt
boilerplate routinely names the happy path ("Sollten wir Sie zu einem
Vorstellungsgespräch einladen, ...") and sometimes the sad one, so a naive
keyword pass would read every Eingangsbestätigung as an invitation.
"""

import email
import email.policy
import email.utils
import re
from dataclasses import dataclass

from jobdeck.contact_resolve import registrable_domain
from jobdeck.sources.base import strip_html

# Stored body cap: enough for any real HR letter, small enough that a
# pathological message cannot bloat the database. The LLM sees even less
# (its own cap lives beside the prompt).
MAX_BODY_CHARS = 20000

CLASS_ABSAGE = "absage"
CLASS_EINLADUNG = "einladung"
CLASS_EINGANG = "eingang"
CLASS_AUTO = "auto"

# One entry per family: phrases that, alone in their sentence-context, carry
# the verdict. Composed forms on purpose — "leider" alone is absent from many
# real rejections and present in polite receipts ("leider dauert die Prüfung
# derzeit länger"), so every pattern here names the ACT, not the mood.
_ABSAGE_PATTERNS = (
    # --- someone else got it ------------------------------------------------
    # `für einen? anderen?` could never match the neuter or the gendered slash
    # form, and hardcoded the modifier "anderen" — a rejection naming an
    # INTERNAL candidate went straight past it.
    r"für ein(?:e|en)?(?:/n)?\s+ander(?:e|es|en|er)(?:/n)?\s+"
    r"(?:person|besetzung|kandidat|bewerber|mitbewerber|interessent)",
    r"für eine andere (?:kandidatin|bewerberin)",
    r"(?:sich|uns) für (?:einen|eine)[^.!?]{0,25}"
    r"(?:kandidat|bewerber|mitbewerber)\w*\s+entschieden",
    r"zugunsten (?:eines|einer|einem) (?:anderen|anderer)",
    r"anderen (?:kandidaten|bewerbern?|mitbewerbern?) (?:den vorzug|entschieden)",
    r"mit (?:anderen|weiteren) (?:kandidat|bewerber)\w*[^.!?]{0,25}"
    r"\bfort(?:setzen|fahren|zusetzen|zufahren)?\b",
    # "Die Entscheidung ist auf eine Mitbewerberin gefallen" — the Art. 33 GG
    # formula. The (?!sie\b) guard is load-bearing: "Die Entscheidung ist auf
    # Sie gefallen" is the exact opposite.
    r"(?:entscheidung|wahl) ist[^.!?]{0,20}auf (?!sie\b)[^.!?]{0,60}gefallen",
    r"für jemand ander\w+[^.!?]{0,30}entschieden",
    r"(?:uns )?anders entschieden",
    r"\banderweitig entschieden\b",
    r"gegen (?:ihre|deine|die) bewerbung[^.!?]{0,40}entschieden",
    r"gegen (?:sie|dich) entschieden",

    # --- the post is gone ---------------------------------------------------
    # An entirely missing family, and the highest-yield one in the audit. The
    # negative lookaheads keep it off "die Stelle soll besetzt werden" and
    # "die Stelle ist neu zu besetzen", which are receipts, not rejections.
    r"\b(?:stelle|position|vakanz|ausschreibung|stellenausschreibung)\b"
    r"(?:(?!\bnicht\b|\bsoll\b|\bwird\b)[^.!?]){0,70}"
    r"\b(?:anderweitig|intern|extern|bereits|inzwischen|zwischenzeitlich|"
    r"mittlerweile|schon|erfolgreich)\b(?:(?!\bnicht\b)[^.!?]){0,30}"
    r"\b(?:besetzt|vergeben)\b",
    r"\b(?:stelle|position|vakanz|ausschreibung|projekt|suche)\b[^.!?]{0,40}"
    r"\b(?:zurückgezogen|gestoppt|storniert)\b",
    r"\b(?:stellen wir|stellen)\b[^.!?]{0,30}\bniemanden\b[^.!?]{0,15}\bein\b",

    # --- the plain no -------------------------------------------------------
    r"\babsagen?\b",
    # The separable verb "\babsagen?\b" structurally cannot see.
    r"sagen (?:wir )?(?:ihnen|dir) (?:hiermit|daher|deshalb|somit|leider)"
    r"[^.!?]{0,20}\bab\b",
    r"(?<!nicht )\babgelehnt\b",
    r"\b(?:bewerbung|auswahlverfahren|bewerbungsverfahren)\b[^.!?]{0,60}"
    r"nicht erfolgreich",
    r"bewerbung[^.!?]{0,30}nicht erfolgreich",
    r"\b(?:es|das) hat (?:diesmal |leider )*nicht (?:geklappt|gereicht)\b",
    r"\bnegativ(?:en|es|e|er)?\s+"
    r"(?:bescheid|rückmeldung|entscheidung|antwort|nachricht)\b",
    r"keine?\s+positive[nrs]?\s+"
    r"(?:rückmeldung|nachricht|antwort|entscheidung|zusage)",
    r"nicht positiv (?:bescheid|bewert)",
    r"\bkeine (?:zusage|zusagen)\b",
    r"\b(?:ihrer|deiner) bewerbung\b[^.!?]{0,40}\bnicht\b[^.!?]{0,20}\bentsprechen\b",

    # --- not considered / not selected --------------------------------------
    r"nicht (?:weiter[- ]?)?berücksichtig",
    r"keine berücksichtigung",
    r"\bberücksichtigung\b[^.!?]{0,50}\bnicht\b",
    r"\b(?:sie wurden|sie sind|wir haben sie)\b[^.!?]{0,40}\bnicht\b"
    r"[^.!?]{0,25}\b(?:ausgewählt|ausgesucht|vorgesehen)\b",
    r"nicht für (?:die|den|das) (?:nächste[nr]?|weitere[nr]?|zweite[nr]?)\s+"
    r"(?:auswahl)?\w*(?:runde|schritt|phase|stufe)",
    r"nicht in die engere (?:wahl|auswahl)",
    r"\bnicht (?:weiter )?im (?:auswahl|bewerbungs)?verfahren\b",
    # The infinitive "zu" the old `nicht (?:weiter)?verfolgen` could not span.
    r"nicht\s+weiter\s*(?:zu\s+)?verfolg",
    r"\bvon (?:einer|der) weiteren "
    r"(?:verfolgung|bearbeitung|berücksichtigung|prüfung)\b",
    r"nicht auf (?:sie|dich) zurück",

    # --- the profile did not fit -------------------------------------------
    r"anforderungsprofil.{0,60}(?:besser|eher|mehr) entsproch",
    r"nicht (?:dem|ihrem|unserem|diesem|unseren) anforderungsprofil",
    r"anforderungsprofil\b[^.!?]{0,30}nicht (?:vollständig )?(?:entspr|erfüll)",
    r"(?:entspr\w+|passen|passt)[^.!?]{0,25}nicht[^.!?]{0,30}anforderungsprofil",
    r"\bpass(?:t|en)\b[^.!?]{0,40}\bnicht\b[^.!?]{0,40}"
    r"\b(?:anforderung|anforderungsprofil|profil|position|stelle)",
    r"\b(?:bewerbung|unterlagen|profil|lebenslauf)\b[^.!?]{0,40}"
    r"nicht (?:vollständig )?überzeug",
    r"\bnicht zu ihren gunsten\b",
    r"\bzu ihren ungunsten\b",

    # --- nothing to offer ---------------------------------------------------
    r"keine (?:passende )?(?:stelle|position|vakanz) anbieten",
    # German puts the whole qualification between the negation and the
    # noun: "keine Ihren Kenntnissen und Faehigkeiten entsprechende
    # Position anbieten" is 47 characters wide.
    r"\bkeine[nr]?\b[^.!?]{0,60}"
    r"\b(?:perspektive|möglichkeit|verwendung|tätigkeit|stelle|position|vakanz)\b"
    r"[^.!?]{0,40}\b(?:anbieten|bieten|in aussicht stellen|gefunden)\b",
    r"\b(?:einstellung|zusammenarbeit|beschäftigung|übernahme)\b[^.!?]{0,80}"
    r"\bnicht\b[^.!?]{0,30}\b(?:in betracht|zustande|möglich|erfolgen)\b",
    # The headhunter idiom. Adjacent on purpose: with a gap it bridged
    # "die Stelle ist noch nicht besetzt und wir würden Sie gerne vorstellen".
    r"\bnicht\s+(?:mehr\s+)?vor(?:zu)?(?:schlagen|stellen)\b",
    r"\bkeine (?:möglichkeit|chance)\b[^.!?]{0,40}vor(?:zu)?(?:schlagen|stellen)",

    # --- the refused invitation, in the rejection family --------------------
    # These read as invitations to the einladung family; naming them here
    # makes the mail two-family, so it yields no verdict and asks him.
    r"nicht zu (?:einem|einer)\s+"
    r"(?:vorstellungsgespräch|kennenlerngespräch|gespräch|interview|termin)",
    r"einladung[^.!?]{0,60}nicht[^.!?]{0,20}"
    r"(?:aussprechen|zusenden|schicken|anbieten)",
    r"gegen (?:eine|die) einladung",

    # --- the closing formulas ----------------------------------------------
    r"leider mitteilen",
    r"\bleider\b[^.!?]{0,40}\bmitteilen\b",
    r"bedauern.{0,40}mitteilen",
    r"alles gute für (?:ihren|deinen|ihre|deine)",
    r"\balles gute für sie\b",
    r"beruflich(?:en|e)?.{0,30}(?:werdegang|zukunft|weg).{0,30}alles gute",
    r"\b(?:weiteren|beruflichen|persönlichen|künftigen)\s+"
    r"(?:weg|werdegang|zukunft|lebensweg)\b[^.!?]{0,25}\balles gute\b",
    r"\b(?:senden|schicken|reichen) wir ihnen\b[^.!?]{0,60}\bzurück\b",
    r"\brücksendung (?:ihrer|der) (?:bewerbungs)?unterlagen\b",
    # --- English -----------------------------------------------------------
    # These rules were written for German mail, and English arrives anyway:
    # an ATS rejection reading "we regret to inform you that we were unable
    # to consider your application" hit NOTHING, so the receipt arm filed it
    # as "your application arrived" — the opposite of what it said.
    r"\bwe regret to inform you\b",
    r"\b(?:un(?:able|fortunately)|not able)[^.!?]{0,40}\b(?:consider|move forward|proceed)\b",
    r"\bwe (?:will not|won't|cannot|can't) (?:be )?(?:mov(?:e|ing) forward|proceed)",
    r"\b(?:decided|chosen) to (?:move forward|proceed) with (?:other|another)",
    r"\byour application (?:was|has been) unsuccessful\b",
)

_EINLADUNG_PATTERNS = (
    r"vorstellungsgespräch",
    r"kennenlern(?:gespräch|termin|en)",
    r"laden (?:wir )?(?:sie|dich).{0,40}ein\b",
    r"\beinladung\b",
    r"\beinladen\b",
    r"terminvorschl[aä]g",
    r"gesprächstermin",
    r"(?:telefonisches|persönliches|erstes|kurzes) (?:gespräch|interview|telefonat)",
    # "interview" only where German grammar puts it. A bare \binterview\b
    # matched English mail these German rules were never meant to judge: a
    # receipt reading "we'll be in touch about interviews" was proposed as
    # an invitation on the first real read of his mailbox.
    r"(?:zum|zu einem|für ein|das) interview",
    r"wann (?:hätten|haben) sie zeit",
)

# A statement that the application ARRIVED — real receipt evidence.
_EINGANG_PATTERNS = (
    r"eingangsbestätigung",
    r"(?:bewerbung|unterlagen).{0,60}(?:eingegangen|erhalten|angekommen)",
    r"bestätigen.{0,15}den (?:eingang|erhalt)",
    r"(?:eingang|erhalt) (?:ihrer|deiner) (?:bewerbung|unterlagen)",
    r"ihre bewerbung.{0,40}(?:prüfen|in bearbeitung|sichten)",
    # "danke für die Zusendung Ihrer Bewerbungsunterlagen" — the phrasing
    # two of the first five real receipts used, and one none of the earlier
    # patterns reached: it names the SENDING rather than the arrival.
    r"zusendung (?:ihrer|deiner) bewerbungsunterlagen",
    # --- English -----------------------------------------------------------
    # Only STATEMENTS, never the opener: "thank you for your application" is
    # the English courtesy line and belongs below with its German twin, or
    # every English rejection would read as a receipt again.
    r"\byour application has (?:landed|arrived|been received)\b",
    r"\bwe(?:'ve| have) received your application\b",
    r"\b(?:is|are|will be)[^.!?]{0,20}\breview(?:ing)?\b[^.!?]{0,30}\byour application\b",
    r"\breview(?:s|ing)? (?:your )?applications?\b[^.!?]{0,25}\bcarefully\b",
    r"\bcarefully review(?:ing)?\b[^.!?]{0,25}\b(?:your )?applications?\b",
)

# The thank-you opener. EVERY German reply starts this way — a rejection, an
# invitation and a receipt alike — so it may carry a verdict only when
# nothing else did, and it must never compete with one. Treating it as
# evidence made every classic rejection look like two families disagreeing.
_COURTESY_PATTERNS = (
    r"danken? (?:ihnen |dir )?für (?:ihre|deine) bewerbung",
    r"vielen dank für (?:ihre|deine) bewerbung",
    # The English twin, and it opens a rejection exactly as often: every one
    # of his English rejections begins "Thank you very much for your
    # application" before turning it down.
    # `thanks?` covers "Thank" and "Thanks" alike, so one pattern is the whole
    # opener; a second spelled-out "thank you" variant was redundant and
    # survived its own deletion, which is how it was found.
    r"\bthanks?(?: you| so much| very much)?[^.!?]{0,30}"
    r"\bfor (?:your )?(?:applying|application)\b",
    r"\bfor taking the time to apply\b",
)

_FAMILIES = (
    (CLASS_ABSAGE, _ABSAGE_PATTERNS),
    (CLASS_EINLADUNG, _EINLADUNG_PATTERNS),
    (CLASS_EINGANG, _EINGANG_PATTERNS),
)

_COMPILED = tuple(
    (family, tuple(re.compile(p, re.IGNORECASE) for p in patterns))
    for family, patterns in _FAMILIES
)
_COURTESY = tuple(re.compile(p, re.IGNORECASE) for p in _COURTESY_PATTERNS)

# A sentence headed by one of these describes a possibility, not an act.
#
# These are SUBORDINATING CONJUNCTIONS, not verb moods. The first version
# also screened "würden"/"könnten", which is wrong twice over: German
# business prose uses the Konjunktiv for politeness, so "Gerne würden wir
# Sie zu einem Vorstellungsgespräch einladen" is a real invitation — and
# screening it left the mail's thank-you opener as the only surviving hit,
# so a genuine interview invitation filed itself as a receipt.
_CONDITIONAL = re.compile(
    r"\b(?:sollten?|falls|sofern|soweit|andernfalls|ggf\.?|"
    r"gegebenenfalls|eventuell|im nächsten schritt|möglicherweise)\b"
    r"|\bim fall(?:e)?\b",
    re.IGNORECASE,
)

# "absagen" is two words wearing one spelling: the noun is a refusal, the
# verb also means to call an event off. Narrowing the phrase itself was tried
# and withdrawn — requiring the addressee ("Ihnen ... absagen") loses the
# CANCELLED INTERVIEW, which then reads as an invitation, and that is far
# worse than the case being fixed. So the phrase stays broad and the message
# decides: an employer who names an event and never once names the
# application is not writing about the application.
_BARE_ABSAGEN = re.compile(r"absagen?", re.IGNORECASE)
_EVENT = re.compile(
    r"\b(?:messe|karrieremesse|jobmesse|veranstaltung|event|webinar|"
    r"infoabend|infotag|vortrag|workshop|sprechstunde|konferenz|"
    r"schulung|seminar)\b", re.IGNORECASE)
_APPLICATION = re.compile(
    r"\b(?:bewerbung|bewerbungen|bewerbungsunterlagen|unterlagen|stelle|"
    r"stellen|position|vakanz|kandidat|bewerber|auswahlverfahren)\w*\b",
    re.IGNORECASE)

# A negation sharing a sentence with invitation vocabulary. German builds a
# rejection out of the invitation's own words — "wir können Sie leider nicht
# einladen", "eine Einladung können wir Ihnen nicht aussprechen" — and no
# amount of invitation phrasing distinguishes the two. Used to demote, never
# to suppress: see classify().
_NEGATION = re.compile(
    r"\bnicht\b|\bkein(?:e[nmrs]?)?\b|\babsehen\b|\bleider nicht\b",
    re.IGNORECASE,
)

# Sentences end at punctuation or a paragraph break — NOT at every newline:
# real mail arrives hard-wrapped at ~72 columns, and splitting on the wrap
# both breaks multi-line phrases and detaches a sentence's opening "Sollten"
# from the clause it conditions, which defeats the conditional screen.
_SOFT_WRAP = re.compile(r"(?<!\n)\n(?!\n)")
_SENTENCE_SPLIT = re.compile(r"[.!?]+|\n{2,}")

# Real mail does not use the space character these patterns are written with.
# Two thirds of the phrases above contain a literal space, so anything a mail
# client puts between two words instead of U+0020 silently deletes the whole
# verdict: "leider mitteilen" matches, "leider mitteilen" matches nothing
# at all, and the mail lands on the review pile looking unclassifiable. The
# producers are ordinary — Word and Outlook emit NO-BREAK SPACE around
# German punctuation, HTML-to-text conversion leaves double spaces and
# zero-width characters, justified HTML mail inserts SOFT HYPHEN inside long
# German compounds ("Vorstellungs­gespräch"), and CRLF arrives on the wire.
# Every real message in the owner's mailbox carried at least one of these.
# soft hyphen, ZWSP/ZWNJ/ZWJ, word joiner, BOM
_ZERO_WIDTH = re.compile("[\\u00ad\\u200b-\\u200d\\u2060\\ufeff]")
# tab, NBSP, ogham space, the en/em quad family, narrow NBSP, ideographic
_HORIZONTAL_SPACE = re.compile(
    "[\\t\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]")
_SPACE_RUN = re.compile(r" {2,}")


def normalize_whitespace(text: str) -> str:
    """Reduce a mail's whitespace to what the patterns are written against.

    Zero-width characters are DELETED (they sit inside words); every other
    exotic space becomes U+0020 (they sit between them). Paragraph breaks
    survive, because the sentence split and the conditional screen are what
    keep a receipt's "Sollten wir Sie einladen" from reading as an invitation.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ZERO_WIDTH.sub("", text)
    text = _HORIZONTAL_SPACE.sub(" ", text)
    return _SPACE_RUN.sub(" ", text)

# Subject prefixes a mail system stamps on an absence answer. These override
# the families: an out-of-office body may well contain "Vorstellungsgespräch"
# in its delegation note, and it still answers nothing.
_AUTO_SUBJECT = re.compile(
    r"^(?:re:\s*|aw:\s*|wg:\s*|fwd?:\s*)*"
    r"(?:automatische antwort|automatic reply|autoreply|auto-reply|"
    r"abwesenheit|abwesenheitsnotiz|out of office|ooo\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RuleVerdict:
    """What the rules concluded, the phrase that carried it, and whether the
    conclusion is safe to act on without a human.

    `confident` is False whenever the verdict only survived because the
    conditional screen removed a competing family's hit, or because two
    families had to be ranked against each other. Those verdicts are still
    shown — as PROPOSALS. The distinction exists because the screen is a
    heuristic over German prose and every gap in it is a silently wrong
    status; making the verdict depend on it cost a click instead.

    'eingang' is exempt: it says only "your application arrived", which is
    what the form path already writes by hand, and it can be corrected by
    any later answer. Only the verdicts that CLOSE a question — absage and
    einladung — must stand without the screen's help.
    """

    classification: str
    pattern: str
    confident: bool = True


def _hits(sentences: list[str], screened: bool) -> dict[str, tuple[str, str]]:
    """First matching phrase per family, with the sentence that carried it.

    `screened` skips conditional sentences (the verdict) or keeps them (what
    the screen suppressed). The sentence travels with the phrase because a
    negation next to the phrase changes what it means — see _NEGATION.
    """
    found: dict[str, tuple[str, str]] = {}
    for family, patterns in _COMPILED:
        for sentence in sentences:
            if screened and _CONDITIONAL.search(sentence):
                continue
            for pattern in patterns:
                match = pattern.search(sentence)
                if match:
                    found.setdefault(family, (match.group(0), sentence))
                    break
            if family in found:
                break
    return found


def _drop_event_cancellation(
        hits: dict[str, tuple[str, str]],
        text: str) -> dict[str, tuple[str, str]]:
    """Withdraw a rejection resting ONLY on the bare verb, in a mail that
    names an event and never names the application.

    Deliberately narrow on both sides: it engages only when the whole
    rejection case is the word "absagen" itself, and only when nothing in
    the message refers to an application. A real rejection always names one
    somewhere, and a cancelled INTERVIEW is not touched — no interview word
    is an event word here, precisely so that mail keeps its verdict.
    """
    hit = hits.get(CLASS_ABSAGE)
    if (hit is not None
            and _BARE_ABSAGEN.fullmatch(hit[0])
            and _EVENT.search(text)
            and not _APPLICATION.search(text)):
        return {k: v for k, v in hits.items() if k != CLASS_ABSAGE}
    return hits


# Where the employer stops writing and the quoted history begins. A reply
# carries the whole conversation below it, and his OWN application is in
# there — "Über ein kurzes Gespräch würde ich mich sehr freuen" is a sentence
# HE wrote, and read as the employer's it filed a rank-4 Einladung onto a
# mail that actually asked him to re-apply through a portal. Anything below
# the first marker belongs to somebody else's turn.
_QUOTE_START = re.compile(
    r"^\s*(?:"
    r"-{2,}\s*(?:ursprüngliche nachricht|original message|"
    r"weitergeleitete nachricht|forwarded message)\s*-{2,}"
    r"|_{5,}"
    r"|>"
    r"|(?:am|on)\s.{0,80}?\s(?:schrieb|wrote)\b"
    r"|von:\s*\S"
    r"|from:\s*\S"
    r"|gesendet:\s*\S"
    r")",
    re.IGNORECASE | re.MULTILINE)


def strip_quoted(text: str) -> str:
    """Everything the sender wrote above the quoted conversation.

    Returns '' when the sender wrote nothing of their own — a bare forward.
    That is deliberate: no verdict at all is honest, and the review pile
    exists for it. Guessing from the quote is how his own words became an
    employer's invitation.
    """
    match = _QUOTE_START.search(text or "")
    return (text[:match.start()] if match else text or "").strip()


def classify(subject: str, body: str) -> RuleVerdict | None:
    """The rule layer's verdict, or None where honesty requires a human.

    Exactly one competing family wins. A mail matching two (an interview
    being cancelled, a rejection that also confirms receipt) is one the
    rules do not understand, and the wrong cheap answer there is a wrong
    STATUS. The thank-you opener never competes — see _COURTESY_PATTERNS.
    """
    subject = normalize_whitespace(subject or "").strip()
    if _AUTO_SUBJECT.search(subject):
        return RuleVerdict(CLASS_AUTO, "Betreff: automatische Antwort")
    text = normalize_whitespace(f"{subject}\n\n{strip_quoted(body)}")
    # The wrap becomes a space, which can meet the space already there.
    text = _SPACE_RUN.sub(" ", _SOFT_WRAP.sub(" ", text))
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    hits = _drop_event_cancellation(_hits(sentences, screened=True), text)
    if len(hits) == 1:
        family, (phrase, sentence) = next(iter(hits.items()))
        # Confident unless the screen is what removed the competition.
        unscreened = _hits(sentences, screened=False)
        confident = (family == CLASS_EINGANG
                     or set(unscreened) == set(hits))
        if family == CLASS_EINLADUNG and _NEGATION.search(sentence):
            # "Wir können Sie leider nicht zu einem Gespräch einladen" is a
            # REJECTION built entirely out of invitation vocabulary, and the
            # family has no way to tell it from the real thing. Filed
            # confidently it was the worst outcome the rules can produce: an
            # Einladung is rank 4, so it closes the application AND, by the
            # anti-downgrade rule, blocks the true Absage arriving behind it.
            # Demoted rather than suppressed — a genuine invitation that
            # happens to say "nicht" ("das Gespräch findet nicht in unserer
            # Zentrale statt") then costs a click instead of vanishing.
            confident = False
        return RuleVerdict(family, phrase, confident)
    if hits:
        # More than one family. A rejection that also states receipt is the
        # one pair with a settled reading, and it is still only a proposal:
        # the same shape is produced by a receipt that merely NAMES a
        # possible rejection, and the two are not distinguishable here.
        if set(hits) == {CLASS_ABSAGE, CLASS_EINGANG}:
            return RuleVerdict(CLASS_ABSAGE, hits[CLASS_ABSAGE][0],
                               confident=False)
        return None
    for pattern in _COURTESY:
        for sentence in sentences:
            match = pattern.search(sentence)
            if match:
                # Nothing but the polite opener — and EVERY German reply
                # opens that way, a rejection included. So this is the
                # weakest evidence in the module, and it must never write a
                # status: an unrecognised rejection would file itself as
                # "In Bearbeitung", wear the Offen label, and skip the review
                # pile entirely, so nothing would ever ask about it again.
                # A receipt that really is one almost always states the
                # arrival too, and that path stays confident.
                return RuleVerdict(CLASS_EINGANG, match.group(0),
                                   confident=False)
    return None


def is_auto_submitted(headers: dict[str, str]) -> bool:
    """Machine-answer markers, consulted only when the rules found nothing.

    Only then, deliberately: an Eingangsbestätigung is itself auto-generated
    and must keep its verdict, and ATS rejections leave from no-reply
    machines too. What these markers settle is the OOO / newsletter residue
    that would otherwise land on the review pile."""
    auto_submitted = headers.get("auto-submitted", "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True
    precedence = headers.get("precedence", "").strip().lower()
    if precedence in ("auto_reply", "bulk", "junk"):
        return True
    if "x-autoreply" in headers:
        return True
    if "x-auto-response-suppress" in headers:
        return True
    if "list-unsubscribe" in headers:
        return True
    return False


# Who must have stamped the verdict this app trusts. Gmail prepends its own
# Authentication-Results line, and `get_message_metadata` keeps the FIRST
# occurrence — but a sender can include a line of their own, so the
# authserv-id is checked rather than merely assumed to be on top.
_GMAIL_AUTHSERV = re.compile(r"^\s*(mx\.google\.com|google\.com)\s*;",
                             re.IGNORECASE)
_DMARC_PASS = re.compile(r"\bdmarc\s*=\s*pass\b", re.IGNORECASE)


def is_bulk_mailing(headers: dict[str, str]) -> bool:
    """A mailing rather than a message written to him.

    Narrower than `is_auto_submitted`: only the markers that mean "this went
    to a list", so it can gate the receipt arm without demoting the
    auto-generated confirmations an ATS sends to one applicant."""
    precedence = headers.get("precedence", "").strip().lower()
    return "list-unsubscribe" in headers or precedence in ("bulk", "list")


def sender_authenticated(headers: dict[str, str]) -> bool:
    """Whether Gmail's own Authentication-Results vouches for the FROM domain.

    DMARC, not SPF or DKIM alone. That distinction is the whole value of the
    check: SPF authenticates the envelope sender (`smtp.mailfrom`) and DKIM
    authenticates whatever domain signed (`d=`), and NEITHER binds the From
    header a human reads. Anyone with a mailbox of their own passes both for
    their own domain while writing any From they like. DMARC is the verdict
    that requires an authenticated identity ALIGNED with the From domain, so
    it is the only one that supports "this really came from that employer".

    Read, never computed — and only from the line Gmail itself stamped.
    """
    verdict = headers.get("authentication-results", "")
    if not _GMAIL_AUTHSERV.match(verdict):
        return False
    return bool(_DMARC_PASS.search(verdict))


def extract_text(raw: bytes) -> str:
    """The message's readable text: plain part first, stripped HTML second.

    The email stdlib under policy.default walks arbitrarily nested
    multiparts and honors charset and transfer encoding by construction.
    Any parse failure yields '' — the caller still holds Gmail's snippet,
    and a broken message must cost one row's body, never the pass."""
    try:
        message = email.message_from_bytes(raw, policy=email.policy.default)
        part = message.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        content = part.get_content()
        if not isinstance(content, str):
            return ""
        if part.get_content_type() == "text/html":
            content = strip_html(content)
        return content.strip()[:MAX_BODY_CHARS]
    except Exception:  # noqa: BLE001 — hostile input; one bad MIME ≠ a dead pass
        return ""


# Where a German letter actually begins. Everything before the salutation is
# furniture: the sender's letterhead, his own postal address, file numbers,
# fax lines. A formal reply from a public authority puts hundreds of
# characters of it in front of the sentence that matters.
_SALUTATION = re.compile(
    r"^\s*(sehr geehrte[rs]?\b|guten (?:tag|morgen)\b|hallo\b|liebe[rs]?\b"
    r"|hi\b|dear\b)",
    re.IGNORECASE | re.MULTILINE,
)


def letter_body(text: str) -> str:
    """The letter without its letterhead, for an excerpt worth reading.

    The invitation that prompted this showed 'Postfach 13 20 | Telefon
    0651 …' where the room and the time should have been — the appointment
    was four hundred characters past the start."""
    if not text:
        return ""
    match = _SALUTATION.search(text)
    return text[match.start():].lstrip() if match else text


def from_address(header: str) -> str:
    """The bare address out of a From header, lowercased; '' when absent."""
    _name, addr = email.utils.parseaddr(header or "")
    return addr.strip().lower()


def from_display_name(header: str) -> str:
    """The human name out of a From header, whitespace-collapsed."""
    name, _addr = email.utils.parseaddr(header or "")
    return " ".join((name or "").split())


# German-market freemail providers: a recruiter writing from a private
# address must never DOMAIN-match every application whose contact happens to
# share the provider. Exact-address matches are unaffected.
FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com",
    "gmx.de", "gmx.net", "gmx.at", "gmx.ch",
    "web.de", "t-online.de", "freenet.de", "mail.de",
    "outlook.com", "outlook.de", "hotmail.com", "hotmail.de",
    "live.com", "live.de", "msn.com",
    "yahoo.com", "yahoo.de", "ymail.com",
    "icloud.com", "me.com", "mac.com", "aol.com",
    "posteo.de", "mailbox.org", "proton.me", "protonmail.com", "pm.me",
})


def matchable_domain(addr: str) -> str:
    """The registrable domain an address may be matched on; '' when none.

    '' for freemail on purpose — see FREEMAIL_DOMAINS — and for anything the
    PSL-backed parser refuses (homographs, IP literals)."""
    domain = addr.rpartition("@")[2].strip().lower()
    if not domain:
        return ""
    registrable = registrable_domain(domain)
    if not registrable or registrable in FREEMAIL_DOMAINS:
        return ""
    return registrable


_LEGAL_FORM = re.compile(
    r"\b(?:gmbh|mbh|ag|kg|kgaa|ohg|ug|se|e\s?k|e\s?v|gbr|"
    r"co|company|holding|group|gruppe|deutschland|international|"
    r"and|und|the)\b", re.IGNORECASE)
_NOT_ALNUM = re.compile(r"[^a-z0-9]+")
# Below this, a prefix comparison is noise: "IT GmbH" would match "italia.de".
_MIN_COMPANY_KEY = 6


def company_key(name: str) -> str:
    """A company name reduced to what survives into a domain.

    Legal forms and punctuation go — 'Müller & Co. KG' and 'mueller-kg.de'
    have to meet somewhere — and the umlauts are transliterated the way a
    German registrar does it, because that is what the domain will spell.
    """
    lowered = (name or "").lower()
    for umlaut, plain in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        lowered = lowered.replace(umlaut, plain)
    return _NOT_ALNUM.sub("", _LEGAL_FORM.sub(" ", lowered))


def company_in_sender(firma: str, from_header: str, from_addr: str) -> bool:
    """Whether the sender plausibly IS this company.

    The weakest arm in the cascade by design, and the only one that reaches a
    form application — those carry no address at all, so nothing else can
    ever tie a reply to them. Measured against the applications whose true
    address IS known: the company name is recognisable in the sender's domain
    in 30 of 35. It therefore proposes and never writes: `matched_by` is not
    in the tier that may set a status.

    Freemail is refused through matchable_domain — half the small employers
    in a mailbox write from gmx.de, and the domain says nothing about who
    they are.
    """
    key = company_key(firma)
    if len(key) < _MIN_COMPANY_KEY:
        return False
    domain = matchable_domain(from_addr)
    if domain:
        stem = company_key(domain.split(".")[0])
        if stem and (key.startswith(stem[:_MIN_COMPANY_KEY])
                     or stem.startswith(key[:_MIN_COMPANY_KEY])):
            return True
    # "Personalabteilung Beispiel GmbH <no-reply@ats-vendor.com>" — an ATS
    # sends from its own domain and puts the employer in the display name.
    display = from_header.rpartition("<")[0] or from_header
    display_key = company_key(display)
    return len(display_key) >= _MIN_COMPANY_KEY and key in display_key


def refnr_in_text(refnr: str, subject: str, body: str) -> bool:
    """Whether a posting's reference number appears literally in the mail.

    Length-screened: short references collide with dates and postcodes, and
    this check helps AUTHORIZE an automatic ledger write."""
    needle = (refnr or "").strip()
    if len(needle) < 5:
        return False
    haystack = f"{subject}\n{body}".lower()
    return needle.lower() in haystack
