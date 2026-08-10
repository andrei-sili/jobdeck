"""Duplicate detection for applications and discovered job postings.

Comparison is done in Python with str.casefold(), which handles German
umlauts and ß correctly — unlike SQLite's built-in lower(), which only
folds ASCII A-Z.
"""

import sqlite3
import unicodedata

# Characters a company writes for DECORATION, never to say which company it
# is. Dropping them is what makes an employer who prints a registered-symbol
# after its name the same employer that answered an earlier application:
#   Cf  invisible format marks — a soft hyphen inside a scraped title, a
#       zero-width space, a bidi mark; by definition nothing is rendered
#   Cc  stray control bytes (real whitespace is handled before this)
#   So  ® © ™ ℠ and other standalone symbols
#   Sk  free-standing accent marks, e.g. a ring above worn as part of a
#       wordmark — the modifier twin of So
# What is deliberately NOT dropped: ordinary punctuation, because a dot can
# carry identity ('a.b GmbH' is not 'ab GmbH'), and legal forms, because GmbH
# and AG can be two different companies under one name.
_DROP_CATEGORIES = frozenset({"Cf", "Cc", "So", "Sk"})


def norm(text: object) -> str:
    """Normalize text for comparison: drop decoration, fold case and space.

    Two spellings of one company must land on one string, and two companies
    must never land on the same one. Order matters: decoration is dropped
    BEFORE NFKC, or '™' would compatibility-decompose into the letters 'TM'
    and survive as part of the name.
    """
    kept = []
    for char in str(text or ""):
        # A newline between two words is a word boundary; deleting it as a
        # control character would weld 'Dresden\nDresden' into one word.
        if char.isspace():
            kept.append(" ")
        elif unicodedata.category(char) not in _DROP_CATEGORIES:
            kept.append(char)
    # NFKC folds the compatibility spellings apart from the ones above: a
    # non-breaking space, a fullwidth letter, a decomposed umlaut.
    folded = unicodedata.normalize("NFKC", "".join(kept))
    # split() collapses runs and trims — a scraped title carries both.
    return " ".join(folded.split()).casefold()


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
    firma_n = norm(firma)
    email_n = norm(email)
    if not firma_n and not email_n:
        return None
    rows = con.execute(
        "SELECT * FROM bewerbungen ORDER BY gesendet_am DESC, id DESC"
    ).fetchall()
    for row in rows:
        if exclude_id is not None and row["id"] == exclude_id:
            continue
        if firma_n and norm(row["firma"]) == firma_n:
            return dict(row)
        if email_n and norm(row["email"]) and norm(row["email"]) == email_n:
            return dict(row)
    return None


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
