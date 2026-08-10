"""Duplicate detection for applications and discovered job postings.

Comparison is done in Python with str.casefold(), which handles German
umlauts and ß correctly — unlike SQLite's built-in lower(), which only
folds ASCII A-Z.
"""

import sqlite3
import unicodedata

# The ONLY visible characters `norm` deletes: marks that assert a name is a
# trademark without ever saying WHICH one. No employer is called "X" while a
# different one is called "X®", so removing them cannot merge two companies.
#
# Deliberately NOT dropped, though every one of them is "decorative" in some
# font: '°' (a firm really can be called "180°"), free-standing accents, and
# enclosed or pictographic symbols. Deleting whole Unicode symbol categories
# would fold "Ⓐ GmbH" into "Ⓑ GmbH" — and a FALSE duplicate is the worse
# error: a missed one wastes an application, a false one silently forbids a
# real application to a firm he has never written to.
_TRADEMARK_MARKS = frozenset("®©™℠℗")

# Invisible by definition, so they cannot carry identity either: Cf is the
# soft hyphen inside a scraped title, the zero-width space, the bidi marks;
# Cc is a stray control byte. Real whitespace is handled before this.
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Cc"})


def fold(text: object) -> str:
    """Case-fold for SEARCHING — umlaut- and ß-correct, and nothing else.

    Keyword matching and free-text search want every character the source
    wrote; only `norm` may delete any, and only because it answers a question
    about IDENTITY. Keeping them apart also keeps `norm` off the hot path:
    it is a per-character Python loop, and a posting description is large.
    """
    return str(text or "").casefold()


def norm(text: object) -> str:
    """Normalize a NAME for comparison: drop decoration, fold case and space.

    Two spellings of one company must land on one string, and two different
    companies must not. Order matters: the marks are dropped BEFORE NFKC, or
    '™' would compatibility-decompose into the letters 'TM' and survive as
    part of the name.

    For matching or searching free text use `fold` — this deletes characters,
    which is only ever right when the question is "is this the same company".
    """
    kept = []
    for char in str(text or ""):
        # A newline between two words is a word boundary; deleting it as a
        # control character would weld 'Dresden\nDresden' into one word.
        if char.isspace():
            kept.append(" ")
        elif char in _TRADEMARK_MARKS:
            continue
        elif unicodedata.category(char) not in _INVISIBLE_CATEGORIES:
            kept.append(char)
    # NFKC folds the compatibility spellings the drop above does not reach:
    # a fullwidth letter, a decomposed umlaut, a Roman numeral.
    folded = unicodedata.normalize("NFKC", "".join(kept))
    # split() collapses runs and trims — a scraped title carries both.
    return " ".join(folded.split()).casefold()


_BEWERBUNGEN_SQL = "SELECT * FROM bewerbungen ORDER BY gesendet_am DESC, id DESC"


def _first_match(
    rows: list, firma: str, email: str, exclude_id: int | None = None
) -> dict | None:
    """The newest application matching this company OR contact e-mail.

    The one definition of "already applied here". Everything that answers that
    question — the send gate, the recording path, the job inbox's warning —
    goes through this, because a second copy of the rule is a screen that
    tells the user one thing while the gate does another."""
    firma_n = norm(firma)
    email_n = norm(email)
    if not firma_n and not email_n:
        return None
    for row in rows:
        if exclude_id is not None and row["id"] == exclude_id:
            continue
        if firma_n and norm(row["firma"]) == firma_n:
            return dict(row)
        if email_n and norm(row["email"]) and norm(row["email"]) == email_n:
            return dict(row)
    return None


def find_duplicate_bewerbung(
    con: sqlite3.Connection,
    firma: str,
    email: str,
    exclude_id: int | None = None,
) -> dict | None:
    """Find an existing application with the same company OR contact email.

    Case-insensitive (umlaut-aware), ignores surrounding whitespace.
    Returns the matching row as a dict, or None.
    """
    return _first_match(con.execute(_BEWERBUNGEN_SQL).fetchall(),
                        firma, email, exclude_id)


def duplicates_for_jobs(con: sqlite3.Connection, jobs: list) -> dict[int, dict]:
    """{job id: the application that already went to this company}.

    Asked per PAGE rather than per row so one scan of the applications serves
    the whole view. `jobs.duplicate_of` cannot answer this: it is written once,
    when the posting is discovered, so every application he sends afterwards
    silently makes more inbox rows lie — 30 of his open postings were at firms
    the gate would already refuse, and the top-ranked one of all had had an
    Absage."""
    rows = con.execute(_BEWERBUNGEN_SQL).fetchall()
    found = {}
    for job in jobs:
        match = _first_match(rows, job["company"], job["contact_email"])
        if match is not None:
            found[job["id"]] = match
    return found


def find_duplicate_job(con: sqlite3.Connection, company: str, title: str) -> dict | None:
    """Find an already-known posting with the same company and title.

    Catches the same job arriving through a second source (each source
    already has a UNIQUE(source, external_id) guard at insert time).
    """
    company_n = norm(company)
    title_n = norm(title)
    if not company_n or not title_n:
        return None
    rows = con.execute(
        "SELECT id, company, title FROM jobs ORDER BY id DESC"
    ).fetchall()
    for row in rows:
        if norm(row["company"]) == company_n and norm(row["title"]) == title_n:
            return dict(row)
    return None
