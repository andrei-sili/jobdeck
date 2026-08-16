"""CSV export of the applications list (Excel-friendly for German systems)."""

import csv
import datetime
from pathlib import Path

from jobdeck import config, db
from jobdeck.constants import DB_COLUMNS

# The characters a spreadsheet reads as "this cell is a formula". A company
# name and a note reach this file straight out of a board feed — `apply_job`
# copies `job['company']` and `job['url']` into the ledger — and the export
# button opens the result in the desktop spreadsheet immediately, so a posting
# whose employer field began with `=` would run as a formula in his own Excel.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: object) -> str:
    """One field, defused: a leading formula character is quoted out.

    A leading apostrophe is what every spreadsheet reads as "this is text" and
    what OWASP recommends here; it costs one visible character in the rare
    cell that starts with a minus sign, and it is the only cheap defence that
    survives the file being opened by a double-click.
    """
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_LEAD) else text


def export_csv() -> Path:
    """Write a UTF-8-BOM, semicolon-delimited CSV (opens cleanly in Excel
    on German systems, umlauts intact). Returns the file path."""
    with db.db() as con:
        rows = db.list_bewerbungen(con)
    stamp = datetime.date.today().strftime("%Y%m%d")
    out_path = config.OUTPUT_DIR / f"applications_export_{stamp}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    keys = [k for k, _ in DB_COLUMNS]
    headers = [label for _, label in DB_COLUMNS]
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(headers)
        for row in rows:
            writer.writerow([_cell(row[k]) for k in keys])
    return out_path
