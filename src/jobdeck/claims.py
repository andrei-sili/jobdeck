"""The register of what a letter may use about the candidate, and how often.

One claim is a permission: a fact bound to the ONE project, employer or
qualification it belongs to. The failure mode this exists to make visible is
not an invented skill — it is a real skill welded to the wrong project, which
reads perfectly and is a lie an interviewer will find.

Since schema v15 a claim also carries the FAMILY it belongs to, where it came
from, and whether the candidate has confirmed it. A fact a model read out of
his profile is a proposal until he says otherwise: the register is the list of
things a letter may say about him, and a reading of his own file is not
authority for that.

Everything here is pure: given the same rows and the same letters it says
the same thing, which is what makes the counters testable without a database
and without the LLM that wrote the letters.
"""

from jobdeck.dedupe import fold

# ---------------------------------------------------------------------------
# The families a claim can belong to (schema v15)
# ---------------------------------------------------------------------------
# One key per family the target architecture names for the candidate
# aggregate. They are kept as a flat vocabulary on one table rather than a
# table per family: the families differ in what they MEAN, not in what the
# register does with them, and nothing yet queries one family in a way the
# others cannot answer. A family that earns its own columns can be given them
# additively later.
#
# Read in the order a German application states them: what he did, then what
# he can, then what proves it, then the terms he works under.
KINDS: dict[str, str] = {
    "experience": "Praxiserfahrung",
    "project": "Projekte",
    "skill": "Technische Kenntnisse",
    "education": "Bildungsweg",
    "credential": "Zertifikate",
    "language": "Sprachen",
    "strength": "Stärken",
    "condition": "Rahmenbedingungen",
}

DEFAULT_KIND = "skill"

# ---------------------------------------------------------------------------
# Verification state (schema v15)
# ---------------------------------------------------------------------------
# `proposed`  — read out of the profile, not yet his word. Never usable.
# `confirmed` — he said this may be claimed.
# `rejected`  — he said it may not. Kept, not deleted, so a second import does
#               not offer back everything he has already refused.
# `superseded`— replaced by a correction. Kept so a correction can never
#               rewrite what an already-written letter was allowed to say.
STATES = ("proposed", "confirmed", "rejected", "superseded")

#: The states the register itself shows. `rejected` and `superseded` are the
#: two answers that are already given, so they stay out of the working view.
VISIBLE_STATES = ("proposed", "confirmed")

SOURCES = ("user", "profile_md")


def normalise_kind(raw: object) -> str:
    """The family key for `raw`, falling back to the strictest family.

    Anything unrecognised becomes a skill: that is what every row in this
    table meant before v15, and it is the family whose rule is tightest (a
    competence may appear only at the one project it is bound to). Filing a
    fact too strictly is visible on the screen and one edit away; filing it
    too loosely is the weld the register exists to prevent.
    """
    key = str(raw or "").strip().lower()
    return key if key in KINDS else DEFAULT_KIND


def kind_label(raw: object) -> str:
    """The family's name, in the German the screen prints."""
    return KINDS[normalise_kind(raw)]


def kind_order(raw: object) -> int:
    """Where the family sorts, so two renders never disagree."""
    return list(KINDS).index(normalise_kind(raw))


def normalise_state(raw: object) -> str:
    """The verification state for `raw`, defaulting to the unverified one.

    An unreadable state must never read as `confirmed`: that is the value
    that would let a fact nobody vouched for into a letter.
    """
    key = str(raw or "").strip().lower()
    return key if key in STATES else "proposed"



def group_by_kind(rows) -> list[tuple[str, str, list]]:
    """The register grouped for reading: (kind, label, rows).

    Families keep their fixed order and an empty one is left out: a heading
    over nothing is a family he has to read past on every visit, and the
    register is meant to be scanned.

    Rows keep the order they arrived in, which is the register's own — the
    grouping decides which heading a row sits under, never where it sits
    beneath it.
    """
    buckets: dict[str, list] = {}
    for row in rows:
        buckets.setdefault(normalise_kind(row["kind"]), []).append(row)
    return [(kind, KINDS[kind], buckets[kind])
            for kind in KINDS if kind in buckets]


def count_proposals(count: int) -> str:
    """"1 Vorschlag" or "N Vorschläge" — a register holding one row must not
    read as though it held several. German inflects; a screen that does not
    is a screen that looks machine-written."""
    return "1 Vorschlag" if count == 1 else f"{count} Vorschläge"


def provenance(claim) -> str:
    """Where a claim came from, as the register states it.

    The register is a list of permissions, and "who said so" is the question
    it has to be able to answer about every row. A section name is kept when
    there is one: "aus profile.md" alone leaves him hunting through the file
    for the sentence he is being asked about.
    """
    if str(claim["source"] or "") != "profile_md":
        return "von dir eingetragen"
    section = str(claim["source_ref"] or "").strip()
    return f"aus profile.md · {section}" if section else "aus profile.md"


# ---------------------------------------------------------------------------
# What the register does NOT yet hold
# ---------------------------------------------------------------------------
def _section_key(name: object) -> str:
    """How two section names are compared: folded, and whitespace collapsed.

    `fold` alone leaves runs of spaces intact, and the provenance string is
    copied out of the file by a model — a doubled space between two words
    would report a gap that does not exist, which is the one direction this
    measurement must not be wrong in.
    """
    return " ".join(fold(str(name or "")).split())


def profile_sections(text: str) -> list[str]:
    """The headings of profile.md, in file order, without their '#'.

    The section is the unit because it is the unit HE wrote in, and it is
    what a claim's provenance names. Duplicated headings collapse: two
    sections with one name cannot be told apart by a provenance string that
    only carries the name.
    """
    seen: set[str] = set()
    headings = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        # '##' and deeper. A single '#' names the FILE — his is "Profil —
        # <his name>" — and counting it as a section puts his own name on a
        # list of things nothing stands for, in a measurement that could
        # then never be complete.
        if not stripped.startswith("##"):
            continue
        heading = stripped.lstrip("#").strip()
        if heading and _section_key(heading) not in seen:
            seen.add(_section_key(heading))
            headings.append(heading)
    return headings


def coverage(sections: list[str], rows) -> dict:
    """Which parts of the profile a confirmed fact already stands for.

    This is the measurement that decides WHEN the register may replace
    profile.md as the factual boundary, rather than a guess that it is ready.
    A section nothing confirmed points at is a part of himself that a letter
    drawing only on confirmed facts would not be able to mention.

    Only confirmed rows count. A proposal standing for a section would make
    the register look ready the moment it was read, which is the one moment
    nobody has checked it.
    """
    covered = {_section_key(row["source_ref"]) for row in rows
               if normalise_state(row["state"]) == "confirmed"}
    covered.discard("")
    missing = [name for name in sections if _section_key(name) not in covered]
    return {"sections": len(sections), "covered": len(sections) - len(missing),
            "missing": missing}


# Where a claim's match terms may be separated. A term may contain spaces
# ("Spring Boot"), so a space is deliberately NOT a separator.
_TERM_SEPARATORS = (",", "\n", ";")


def parse_terms(raw: object) -> list[str]:
    """The words a letter would have to use to be claiming this, in order.

    Case is preserved for display; matching folds. Duplicates are dropped so
    a term written twice cannot make one letter count twice.
    """
    text = str(raw or "")
    for separator in _TERM_SEPARATORS[1:]:
        text = text.replace(separator, _TERM_SEPARATORS[0])
    seen: set[str] = set()
    terms = []
    for part in text.split(_TERM_SEPARATORS[0]):
        term = part.strip()
        if term and fold(term) not in seen:
            seen.add(fold(term))
            terms.append(term)
    return terms


def count_uses(raw_terms: object, letters: list[str]) -> int | None:
    """How many of `letters` claim this — or None when it cannot be counted.

    None and 0 are different answers and the screen must not merge them: a
    claim with no terms has never been LOOKED for, while 0 means it was
    looked for in every letter and found in none. Reporting "never used" for
    an unsearchable claim would invite deleting a permission that is in fact
    being used.
    """
    terms = parse_terms(raw_terms)
    if not terms:
        return None
    folded = [fold(term) for term in terms]
    return sum(1 for letter in letters
               if any(term in fold(letter) for term in folded))


def describe_uses(count: int | None) -> str:
    """The counter as the register states it, in the app's German."""
    if count is None:
        return "nicht zählbar"
    if count == 0:
        return "noch nie"
    if count == 1:
        return "in 1 Brief"
    return f"in {count} Briefen"


def headline(claim) -> str:
    """A claim as one line: the fact and the project it is bound to."""
    fact = str(claim["fact"] or "").strip()
    binding = str(claim["binding"] or "").strip()
    return f"{fact} — {binding}" if binding else fact
