"""Duplicate detection for applications and discovered job postings.

Comparison is done in Python with str.casefold(), which handles German
umlauts and ß correctly — unlike SQLite's built-in lower(), which only
folds ASCII A-Z.
"""

import sqlite3


def norm(text: object) -> str:
    """Normalize text for comparison: strip whitespace, Unicode casefold."""
    return str(text or "").strip().casefold()


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
