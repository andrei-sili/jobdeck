import datetime

from jobdeck.dates import days_since, de_to_iso, heute_de, iso_to_de


def test_iso_to_de_valid():
    assert iso_to_de("2026-06-08") == "08. Juni 2026"
    assert iso_to_de("2026-03-01") == "01. März 2026"


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


def test_days_since():
    today = datetime.date.today()
    assert days_since(today.isoformat()) == 0
    assert days_since((today - datetime.timedelta(days=14)).isoformat()) == 14
    assert days_since("invalid") is None
    assert days_since("") is None
