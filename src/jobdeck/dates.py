"""German date formatting helpers (locale-independent).

Plus `parse_posting_date`, which reads the publication date out of whatever
shape a board sent: the three sources JobDeck polls send three different ones.
"""

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


# A publication date is credible inside this window; anything outside is data,
# not a date. The bound matters: 1784-03-22 is what `date.fromisoformat` makes
# of the epoch string "1784032228", and a posting read as 242 years old is
# indistinguishable from one that really is stale.
_EARLIEST_YEAR = 2000
_LATEST_YEAR = 2100
_EPOCH_MIN = 946_684_800      # 2000-01-01T00:00:00Z
_EPOCH_MAX = 4_102_444_800    # 2100-01-01T00:00:00Z
_MAX_FRACTION_DIGITS = 6      # what every CPython accepts; .NET sends 7


def _from_epoch(raw: str) -> datetime.date | None:
    """A date out of epoch seconds or milliseconds, None when out of range.

    The range check is what keeps the two readings apart: a basic-format date
    ("20260806") is far below _EPOCH_MIN and falls through to the ISO branch,
    while a real epoch is far above any year an ISO parse could invent."""
    try:
        value = int(raw)
    except ValueError:
        return None
    if _EPOCH_MIN <= value <= _EPOCH_MAX:
        seconds = value
    elif _EPOCH_MIN * 1000 <= value <= _EPOCH_MAX * 1000:
        seconds = value // 1000
    else:
        return None
    # UTC, not the local zone: the boards state no offset, and a date that
    # depends on the reader's clock cannot be compared across a stored corpus.
    return datetime.datetime.fromtimestamp(seconds, datetime.UTC).date()


def _from_iso(raw: str) -> datetime.date | None:
    """A date out of an ISO 8601 date or timestamp, None when it is neither.

    The fractional part is truncated to six digits BEFORE parsing: Jooble sends
    seven ('2026-07-13T00:00:00.0000000') and how many digits
    `fromisoformat` tolerates has moved between CPython releases — the CI
    runner is not the local interpreter (slice #3's portability bug)."""
    text = raw
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dot = text.find(".")
    if dot >= 0:
        end = dot + 1
        while end < len(text) and text[end].isdigit():
            end += 1
        keep = min(end, dot + 1 + _MAX_FRACTION_DIGITS)
        text = text[:keep] + text[end:]
    try:
        return datetime.datetime.fromisoformat(text).date()
    except ValueError:
        pass
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def parse_posting_date(raw) -> datetime.date | None:
    """The publication date a board sent, or None when there is none to read.

    One reader for three formats, because each source states the same fact
    differently and a half-read date is worse than an absent one — it sorts:
      * Arbeitsagentur — an ISO date, '2026-06-09'
      * Arbeitnow      — Unix epoch seconds as a string, '1785897635'
      * Jooble         — an ISO timestamp with seven fractional digits

    Epoch is tried first so a ten-digit epoch can never be misread as a
    basic-format date. An implausible year yields None: 'unknown' costs a
    posting nothing downstream, while a wrong date silently reorders the inbox.
    """
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        raw = str(int(raw))
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    # ASCII digits only: str.isdigit() also accepts Arabic-Indic digits, which
    # int() happily converts — a host of digit shapes no board ever sends
    numeric = text.isascii() and text.isdigit()
    parsed = _from_epoch(text) if numeric else None
    if parsed is None:
        parsed = _from_iso(text)
    if parsed is None or not (_EARLIEST_YEAR <= parsed.year <= _LATEST_YEAR):
        return None
    return parsed


def posting_date_iso(raw) -> str:
    """`parse_posting_date` as a storable ISO date, '' when unreadable."""
    parsed = parse_posting_date(raw)
    return parsed.isoformat() if parsed is not None else ""


def days_since(iso_str: str) -> int | None:
    """Days elapsed since the given ISO date; None if the date is invalid."""
    try:
        d = datetime.date.fromisoformat((iso_str or "").strip())
        return (datetime.date.today() - d).days
    except (ValueError, AttributeError):
        return None
