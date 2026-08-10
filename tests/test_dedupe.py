import sqlite3

import pytest

from jobdeck.dedupe import find_duplicate_bewerbung, find_duplicate_job, fold, norm


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


def test_norm_drops_the_marks_that_only_assert_a_trademark():
    # The miss this closes: a posting spelling the employer with a registered
    # symbol, against an application already sent to it without one.
    assert norm("a.b® GmbH") == norm("a.b GmbH") == "a.b gmbh"
    assert norm("ACME™") == norm("ACME©") == norm("ACME℠") == norm("ACME") == "acme"


@pytest.mark.parametrize("left, right, why", [
    ("180° GmbH", "180 GmbH", "a degree sign can be the name itself"),
    ("C^3 GmbH", "C3 GmbH", "'^' is a modifier symbol AND legal in an e-mail"),
    ("Ⓐ GmbH", "Ⓑ GmbH", "enclosed letters are different letters"),
    ("2GRAD˚ GmbH", "2GRAD GmbH", "a free-standing accent is not a trademark mark"),
    ("bewerbung^x@firma.de", "bewerbungx@firma.de", "an address is exact by definition"),
])
def test_norm_never_merges_two_names_that_merely_look_decorated(left, right, why):
    """The worse error direction. A missed duplicate wastes one application; a
    FALSE one silently refuses a real application to a firm he never wrote to
    (services/send.py), so `norm` deletes only marks that cannot name anything.
    Dropping whole Unicode symbol categories folded every pair below."""
    assert norm(left) != norm(right), why


def test_norm_collapses_the_whitespace_a_scraper_leaves_behind():
    assert norm("Beispiel  GmbH") == "beispiel gmbh"
    assert norm("Entwickler in Berlin\nBerlin") == "entwickler in berlin berlin"
    assert norm("Fachinformatiker\xa0(m/w/d)") == "fachinformatiker (m/w/d)"
    assert norm("intel\xadligen\xadte") == "intelligente"  # soft hyphens


def test_norm_drops_what_the_reader_never_sees():
    # Cf and Cc: invisible by definition, so they cannot carry identity.
    assert norm("Bei​spiel GmbH") == "beispiel gmbh"   # zero-width space
    assert norm("Beispiel‪ GmbH") == "beispiel gmbh"   # bidi mark
    assert norm("Beispiel\x00 GmbH") == "beispiel gmbh"     # stray control byte
    assert norm("Beispiel‍ GmbH") == "beispiel gmbh"   # zero-width joiner


def test_norm_folds_the_compatibility_spellings_nfkc_is_for():
    # Pinned because deleting the NFKC call left the suite green otherwise.
    assert norm("ＡＢＣ GmbH") == norm("ABC GmbH") == "abc gmbh"   # fullwidth
    assert norm("Müller GmbH") == norm("Müller GmbH")        # decomposed ü
    assert norm("Ⅳ GmbH") == norm("IV GmbH")                       # Roman numeral


def test_norm_keeps_what_actually_names_a_company():
    # A dot carries identity, and a legal form can tell two companies apart.
    assert norm("a.b GmbH") != norm("ab GmbH")
    assert norm("Müller GmbH") != norm("Müller AG")


def test_norm_decoration_is_dropped_before_nfkc_widens_it():
    # NFKC decomposes '™' into the letters 'TM'; dropping it afterwards would
    # leave 'acmetm' behind and the two spellings would stay different.
    assert "tm" not in norm("ACME™")


def test_fold_is_for_searching_and_deletes_nothing():
    """A search haystack must keep every character the source wrote — and a
    posting description is far too large for norm's per-character loop, which
    runs on the shared event loop in sources/arbeitnow.py."""
    assert fold("MÜLLER GmbH®") == "müller gmbh®"
    assert fold("Straße") == "strasse"
    for hostile in ("a­b", "a​b", "x™", "180°"):
        assert fold(hostile) == hostile.casefold()
    assert fold(None) == ""


def test_the_search_paths_never_reach_the_identity_function():
    """norm answers 'is this the same company'. Using it to match keywords
    both deletes characters from the haystack and costs ~27x per character."""
    import pathlib

    from jobdeck import dedupe
    from jobdeck.sources import arbeitnow
    from jobdeck.ui.pages import applications
    for module in (arbeitnow, applications):
        source = pathlib.Path(module.__file__).read_text()
        body = source[source.index("import"):]
        assert "norm(" not in body, f"{module.__name__} still folds with norm()"
        # The NAME proves nothing on its own: `import norm as fold` satisfies
        # the grep above while restoring exactly the behaviour it forbids.
        assert module.fold is dedupe.fold, (
            f"{module.__name__}.fold is not dedupe.fold")


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
