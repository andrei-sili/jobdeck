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
    r"\babsagen?\b",
    r"nicht (?:weiter[- ]?)?berücksichtig",
    r"keine berücksichtigung",
    r"für einen? anderen? (?:kandidat|bewerber|mitbewerber)",
    r"für eine andere (?:kandidatin|bewerberin)",
    r"anderen (?:kandidaten|bewerbern?|mitbewerbern?) (?:den vorzug|entschieden)",
    r"anforderungsprofil.{0,60}(?:besser|eher|mehr) entsproch",
    r"nicht in die engere (?:wahl|auswahl)",
    r"leider mitteilen",
    r"bedauern.{0,40}mitteilen",
    r"nicht (?:weiter)?verfolgen",
    r"keine (?:passende )?(?:stelle|position|vakanz) anbieten",
    r"alles gute für (?:ihren|deinen|ihre|deine)",
    r"beruflich(?:en|e)?.{0,30}(?:werdegang|zukunft|weg).{0,30}alles gute",
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
    r"\binterview\b",
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
)

# The thank-you opener. EVERY German reply starts this way — a rejection, an
# invitation and a receipt alike — so it may carry a verdict only when
# nothing else did, and it must never compete with one. Treating it as
# evidence made every classic rejection look like two families disagreeing.
_COURTESY_PATTERNS = (
    r"danken? (?:ihnen |dir )?für (?:ihre|deine) bewerbung",
    r"vielen dank für (?:ihre|deine) bewerbung",
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

# Sentences end at punctuation or a paragraph break — NOT at every newline:
# real mail arrives hard-wrapped at ~72 columns, and splitting on the wrap
# both breaks multi-line phrases and detaches a sentence's opening "Sollten"
# from the clause it conditions, which defeats the conditional screen.
_SOFT_WRAP = re.compile(r"(?<!\n)\n(?!\n)")
_SENTENCE_SPLIT = re.compile(r"[.!?]+|\n{2,}")

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


def _hits(sentences: list[str], screened: bool) -> dict[str, str]:
    """First matching phrase per family; `screened` skips conditional
    sentences (the verdict) or keeps them (what the screen suppressed)."""
    found: dict[str, str] = {}
    for family, patterns in _COMPILED:
        for sentence in sentences:
            if screened and _CONDITIONAL.search(sentence):
                continue
            for pattern in patterns:
                match = pattern.search(sentence)
                if match:
                    found.setdefault(family, match.group(0))
                    break
            if family in found:
                break
    return found


def classify(subject: str, body: str) -> RuleVerdict | None:
    """The rule layer's verdict, or None where honesty requires a human.

    Exactly one competing family wins. A mail matching two (an interview
    being cancelled, a rejection that also confirms receipt) is one the
    rules do not understand, and the wrong cheap answer there is a wrong
    STATUS. The thank-you opener never competes — see _COURTESY_PATTERNS.
    """
    if _AUTO_SUBJECT.search(subject or ""):
        return RuleVerdict(CLASS_AUTO, "Betreff: automatische Antwort")
    text = _SOFT_WRAP.sub(" ", f"{subject}\n\n{body}")
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    hits = _hits(sentences, screened=True)
    if len(hits) == 1:
        family, phrase = next(iter(hits.items()))
        # Confident unless the screen is what removed the competition.
        unscreened = _hits(sentences, screened=False)
        confident = (family == CLASS_EINGANG
                     or set(unscreened) == set(hits))
        return RuleVerdict(family, phrase, confident)
    if hits:
        # More than one family. A rejection that also states receipt is the
        # one pair with a settled reading, and it is still only a proposal:
        # the same shape is produced by a receipt that merely NAMES a
        # possible rejection, and the two are not distinguishable here.
        if set(hits) == {CLASS_ABSAGE, CLASS_EINGANG}:
            return RuleVerdict(CLASS_ABSAGE, hits[CLASS_ABSAGE],
                               confident=False)
        return None
    for pattern in _COURTESY:
        for sentence in sentences:
            match = pattern.search(sentence)
            if match:
                # Nothing but the polite opener: it arrived, and that is all
                # this mail says.
                return RuleVerdict(CLASS_EINGANG, match.group(0))
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


def refnr_in_text(refnr: str, subject: str, body: str) -> bool:
    """Whether a posting's reference number appears literally in the mail.

    Length-screened: short references collide with dates and postcodes, and
    this check helps AUTHORIZE an automatic ledger write."""
    needle = (refnr or "").strip()
    if len(needle) < 5:
        return False
    haystack = f"{subject}\n{body}".lower()
    return needle.lower() in haystack
