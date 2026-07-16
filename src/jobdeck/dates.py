"""German date formatting helpers (locale-independent)."""

import datetime

MONATE_DE = [
    "",
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


def heute_de() -> str:
    """Today in German letter format: '4. Juni 2026'.

    DIN 5008: in the written-month form the day carries NO leading zero
    (only the purely numeric 04.06.2026 form does)."""
    h = datetime.date.today()
    return f"{h.day}. {MONATE_DE[h.month]} {h.year}"


def iso_to_de(iso_str: str) -> str:
    """Convert '2026-06-08' -> '8. Juni 2026'; today's date if invalid."""
    try:
        d = datetime.date.fromisoformat((iso_str or "").strip())
        return f"{d.day}. {MONATE_DE[d.month]} {d.year}"
    except (ValueError, AttributeError):
        return heute_de()


def de_to_iso(de_str: str) -> str:
    """Convert '08. Juni 2026' -> '2026-06-08'; today's date if invalid."""
    try:
        parts = de_str.replace(".", "").split()
        day, month, year = int(parts[0]), MONATE_DE.index(parts[1]), int(parts[2])
        return datetime.date(year, month, day).isoformat()
    except (ValueError, IndexError, AttributeError):
        return datetime.date.today().isoformat()


def days_since(iso_str: str) -> int | None:
    """Days elapsed since the given ISO date; None if the date is invalid."""
    try:
        d = datetime.date.fromisoformat((iso_str or "").strip())
        return (datetime.date.today() - d).days
    except (ValueError, AttributeError):
        return None
