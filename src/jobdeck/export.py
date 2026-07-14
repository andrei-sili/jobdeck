"""CSV export of the applications list (Excel-friendly for German systems)."""

import csv
import datetime
from pathlib import Path

from jobdeck import config, db
from jobdeck.constants import DB_COLUMNS


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
            writer.writerow([row[k] for k in keys])
    return out_path
