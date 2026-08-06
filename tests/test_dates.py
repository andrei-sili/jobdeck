import datetime

import pytest

from jobdeck.dates import (
    days_since,
    de_to_iso,
    heute_de,
    iso_to_de,
    parse_posting_date,
    posting_date_iso,
)


def test_iso_to_de_valid():
    assert iso_to_de("2026-06-08") == "8. Juni 2026"
    assert iso_to_de("2026-03-01") == "1. März 2026"


def test_iso_to_de_invalid_falls_back_to_today():
    assert iso_to_de("not a date") == heute_de()
    assert iso_to_de("") == heute_de()
    assert iso_to_de(None) == heute_de()


def test_de_to_iso_valid():
    assert de_to_iso("08. Juni 2026") == "2026-06-08"
    assert de_to_iso("01. März 2026") == "2026-03-01"


def test_de_to_iso_invalid_falls_back_to_today():
    assert de_to_iso("garbage") == datetime.date.today().isoformat()


def test_roundtrip():
    assert de_to_iso(iso_to_de("2026-12-24")) == "2026-12-24"


@pytest.mark.parametrize("raw, expected, why", [
    ("2026-06-09", datetime.date(2026, 6, 9), "arbeitsagentur: a plain ISO date"),
    ("1785897635", datetime.date(2026, 8, 5), "arbeitnow: epoch seconds"),
    ("2026-07-13T00:00:00.0000000", datetime.date(2026, 7, 13),
     "jooble: seven fractional digits"),
    ("2026-07-11T18:39:33.8300000", datetime.date(2026, 7, 11), "jooble, with a time"),
    ("2026-07-11T18:39:33.830", datetime.date(2026, 7, 11), "three digits"),
    ("2026-07-11T18:39:33", datetime.date(2026, 7, 11), "no fraction at all"),
    ("2026-07-11T22:39:33Z", datetime.date(2026, 7, 11), "a Zulu suffix"),
    ("20260806", datetime.date(2026, 8, 6), "ISO basic format is not an epoch"),
    ("1784032228", datetime.date(2026, 7, 14),
     "the epoch that date.fromisoformat reads as 1784-03-22"),
    ("1785897635000", datetime.date(2026, 8, 5), "epoch milliseconds"),
    (1785897635, datetime.date(2026, 8, 5), "an unstringified feed value"),
])
def test_parse_posting_date_reads_every_shape_the_boards_send(raw, expected, why):
    assert parse_posting_date(raw) == expected, why


@pytest.mark.parametrize("raw", [
    "", "   ", None, "not a date", "n/a", [], {}, True,
    "1784-03-22",      # a real ISO date, but no board publishes in 1784
    "1970-01-02",      # an epoch-shaped mistake already turned into a date
    "999999999999999",  # far past any plausible epoch
    "١٧٨٤٠٣٢٢٢٨",       # Arabic-Indic digits: str.isdigit() says yes, int() agrees
])
def test_an_unreadable_date_is_unknown_rather_than_wrong(raw):
    # 'unknown' costs a posting nothing downstream; a wrong date reorders the
    # inbox silently, which is the failure this parser exists to prevent
    assert parse_posting_date(raw) is None
    assert posting_date_iso(raw) == ""


def test_no_epoch_string_is_ever_read_as_an_iso_date():
    """A year of epoch values, every one read as the timestamp it is.

    Two of his real postings hit the shape where `date.fromisoformat` succeeds
    on an epoch string and answers a date in the 1780s. Trying ISO first would
    pass every other case in this file and still reintroduce that.
    """
    misread = []
    for seconds in range(1_760_000_000, 1_792_000_000, 5_000):
        raw = str(seconds)
        expected = datetime.datetime.fromtimestamp(seconds, datetime.UTC).date()
        if parse_posting_date(raw) != expected:
            misread.append(raw)
    assert misread == []


def test_posting_date_iso_is_storable():
    assert posting_date_iso("1785897635") == "2026-08-05"
    assert posting_date_iso("2026-07-13T00:00:00.0000000") == "2026-07-13"


def test_days_since():
    today = datetime.date.today()
    assert days_since(today.isoformat()) == 0
    assert days_since((today - datetime.timedelta(days=14)).isoformat()) == 14
    assert days_since("invalid") is None
    assert days_since("") is None
