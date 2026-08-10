import sqlite3

import pytest

from jobdeck.dedupe import find_duplicate_bewerbung, find_duplicate_job, norm


@pytest.fixture()
def con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE bewerbungen (
            id INTEGER PRIMARY KEY, gesendet_am TEXT, firma TEXT, email TEXT
        )
        """
    )
    con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, company TEXT, title TEXT)")
    con.executemany(
        "INSERT INTO bewerbungen (gesendet_am, firma, email) VALUES (?, ?, ?)",
        [
            ("2026-06-10", "Müller GmbH", "jobs@mueller.de"),
            ("2026-06-11", "ACME AG", ""),
        ],
    )
    con.execute(
        "INSERT INTO jobs (company, title) VALUES (?, ?)",
        ("Müller GmbH", "Python Entwickler (m/w/d)"),
    )
    yield con
    con.close()


def test_norm_handles_umlauts_and_whitespace():
    assert norm("  MÜLLER GmbH ") == "müller gmbh"
    assert norm("Straße") == norm("STRASSE".replace("SS", "ß".upper()))  # ß casefolds to ss
    assert norm(None) == ""


def test_norm_drops_decoration_that_does_not_name_the_company():
    # The miss this closes: a posting spelling the employer with a registered
    # symbol, against an application already sent to it without one.
    assert norm("a.b® GmbH") == norm("a.b GmbH") == "a.b gmbh"
    assert norm("ACME™") == norm("ACME©") == norm("ACME") == "acme"
    assert norm("2GRAD˚") == "2grad"  # Sk, the modifier twin of ®


def test_norm_collapses_the_whitespace_a_scraper_leaves_behind():
    assert norm("Beispiel  GmbH") == "beispiel gmbh"
    assert norm("Entwickler in Berlin\nBerlin") == "entwickler in berlin berlin"
    assert norm("Fachinformatiker\xa0(m/w/d)") == "fachinformatiker (m/w/d)"
    assert norm("intel\xadligen\xadte") == "intelligente"  # soft hyphens


def test_norm_keeps_what_actually_names_a_company():
    # A dot carries identity, and a legal form can tell two companies apart.
    assert norm("a.b GmbH") != norm("ab GmbH")
    assert norm("Müller GmbH") != norm("Müller AG")


def test_norm_decoration_is_dropped_before_nfkc_widens_it():
    # NFKC decomposes '™' into the letters 'TM'; dropping it afterwards would
    # leave 'acmetm' behind and the two spellings would stay different.
    assert "tm" not in norm("ACME™")


def test_duplicate_by_firma_case_insensitive(con):
    dup = find_duplicate_bewerbung(con, "müller gmbh", "")
    assert dup is not None and dup["firma"] == "Müller GmbH"


def test_duplicate_by_email(con):
    dup = find_duplicate_bewerbung(con, "Andere Firma", "JOBS@MUELLER.DE")
    assert dup is not None and dup["firma"] == "Müller GmbH"


def test_no_duplicate(con):
    assert find_duplicate_bewerbung(con, "Neue Firma", "new@firma.de") is None


def test_empty_inputs_never_match(con):
    assert find_duplicate_bewerbung(con, "", "") is None


def test_exclude_id_skips_self(con):
    row = con.execute("SELECT id FROM bewerbungen WHERE firma='Müller GmbH'").fetchone()
    assert find_duplicate_bewerbung(con, "Müller GmbH", "", exclude_id=row["id"]) is None


def test_empty_email_rows_do_not_match_empty_email(con):
    # ACME AG has an empty email; searching an empty email must not match it
    dup = find_duplicate_bewerbung(con, "Sonstige", "")
    assert dup is None


def test_duplicate_job_same_company_title(con):
    dup = find_duplicate_job(con, "MÜLLER GMBH", "python entwickler (m/w/d)")
    assert dup is not None


def test_duplicate_job_different_title(con):
    assert find_duplicate_job(con, "Müller GmbH", "Java Entwickler") is None
