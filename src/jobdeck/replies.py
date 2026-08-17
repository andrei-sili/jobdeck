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

_EINGANG_PATTERNS = (
    r"eingangsbestätigung",
    r"(?:bewerbung|unterlagen).{0,60}(?:eingegangen|erhalten|angekommen)",
    r"bestätigen.{0,15}den (?:eingang|erhalt)",
    r"(?:eingang|erhalt) (?:ihrer|deiner) (?:bewerbung|unterlagen)",
    r"danken? (?:ihnen |dir )?für (?:ihre|deine) bewerbung",
    r"vielen dank für (?:ihre|deine) bewerbung",
    r"ihre bewerbung.{0,40}(?:prüfen|in bearbeitung|sichten)",
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

# A sentence carrying one of these is describing a possibility, not stating
# an act — the boilerplate screen described in the module docstring.
_CONDITIONAL = re.compile(
    r"\b(?:sollten?|falls|ggf\.?|gegebenenfalls|eventuell|"
    r"im nächsten schritt|würden?|könnten?|möglicherweise)\b",
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
    """What the rules concluded, and the phrase that carried it."""

    classification: str
    pattern: str


def classify(subject: str, body: str) -> RuleVerdict | None:
    """The rule layer's verdict, or None where honesty requires a human.

    Exactly-one-family, with every hit inside a conditional sentence
    discarded first. A mail matching two families is a mail the rules do not
    understand (an interview being cancelled, a receipt that genuinely
    invites) — the wrong cheap answer there is a wrong STATUS, so there is
    deliberately no tie-break beyond the conditional screen."""
    if _AUTO_SUBJECT.search(subject or ""):
        return RuleVerdict(CLASS_AUTO, "Betreff: automatische Antwort")
    text = _SOFT_WRAP.sub(" ", f"{subject}\n\n{body}")
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    hits: dict[str, str] = {}
    for family, patterns in _COMPILED:
        for sentence in sentences:
            if _CONDITIONAL.search(sentence):
                continue
            for pattern in patterns:
                match = pattern.search(sentence)
                if match:
                    hits.setdefault(family, match.group(0))
                    break
            if family in hits:
                break
    if CLASS_ABSAGE in hits and CLASS_EINGANG in hits \
            and CLASS_EINLADUNG not in hits:
        # Every German rejection opens by thanking for the application — the
        # receipt vocabulary is part of the rejection's own fixed form and
        # must not be allowed to talk the verdict into "ambiguous".
        del hits[CLASS_EINGANG]
    if len(hits) != 1:
        return None
    family, phrase = next(iter(hits.items()))
    return RuleVerdict(family, phrase)


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


def sender_authenticated(headers: dict[str, str]) -> bool:
    """Whether Gmail's own Authentication-Results vouches for the sender.

    Read, never computed: Gmail evaluated SPF/DKIM on receipt and stamped
    the verdict topmost. The receipt path demands this before it is allowed
    to WRITE an application — a From header is what a forger controls, and
    this is what he cannot."""
    verdict = headers.get("authentication-results", "").lower()
    return "spf=pass" in verdict or "dkim=pass" in verdict


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
