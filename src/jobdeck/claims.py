"""The register of what a letter is allowed to claim, and how often it did.

One claim is a permission: a competence or credential bound to the ONE
project or employer it belongs to. The failure mode this exists to make
visible is not an invented skill — it is a real skill welded to the wrong
project, which reads perfectly and is a lie an interviewer will find.

Everything here is pure: given the same rows and the same letters it says
the same thing, which is what makes the counters testable without a database
and without the LLM that wrote the letters.
"""

from jobdeck.dedupe import fold

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
