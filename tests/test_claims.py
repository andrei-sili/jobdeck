"""The register of what a letter may claim: parsing, counting, storage."""

import pytest

from jobdeck import claims, db


def _add_job(con, **over):
    values = {
        "source": "arbeitsagentur",
        "external_id": over.pop("external_id", "REF-1"),
        "title": "Python Entwickler (m/w/d)",
        "company": "Neue Firma GmbH",
        "url": "https://example.org/job/1",
    }
    values.update(over)
    return db.insert_job_if_new(con, values)


def _letter(con, body, external_id):
    job_id = _add_job(con, external_id=external_id)
    db.upsert_draft(con, job_id, {"status": "ready", "anschreiben_body": body})
    return job_id


# --------------------------------------------------------------------------
# parse_terms
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("FastAPI, PostgreSQL", ["FastAPI", "PostgreSQL"]),
    ("FastAPI\nPostgreSQL;Alembic", ["FastAPI", "PostgreSQL", "Alembic"]),
    ("  FastAPI  ,, PostgreSQL ", ["FastAPI", "PostgreSQL"]),
    ("", []),
    (None, []),
    ("   ", []),
])
def test_parse_terms_splits_on_every_separator(raw, expected):
    assert claims.parse_terms(raw) == expected


def test_a_term_may_contain_spaces():
    """'Spring Boot' is one term. Splitting on whitespace would look for
    'Boot' on its own and count a letter that never mentions Spring."""
    assert claims.parse_terms("Spring Boot, Django REST Framework") == [
        "Spring Boot", "Django REST Framework"]


def test_a_term_written_twice_counts_one_letter_once():
    assert claims.parse_terms("Django, django , DJANGO") == ["Django"]


# --------------------------------------------------------------------------
# count_uses
# --------------------------------------------------------------------------
def test_counts_the_letters_that_claim_it_case_insensitively():
    letters = ["… mit fastapi und Alembic …", "… nur Django …",
               "… FastAPI im Abschlussprojekt …"]
    assert claims.count_uses("FastAPI", letters) == 2


def test_a_letter_naming_two_terms_of_one_claim_counts_once():
    """The claim is one permission, not one per word — a letter that names
    the whole stack must not read as three separate uses."""
    letters = ["… FastAPI, PostgreSQL und Alembic …"]
    assert claims.count_uses("FastAPI, PostgreSQL, Alembic", letters) == 1


def test_no_terms_is_not_the_same_answer_as_never_used():
    """None means nobody looked; 0 means every letter was read and none
    claimed it. Merging them invites deleting a permission that is in use."""
    assert claims.count_uses("", ["… FastAPI …"]) is None
    assert claims.count_uses("FastAPI", []) == 0
    assert claims.count_uses("Kotlin", ["… FastAPI …"]) == 0


def test_the_counter_states_which_of_the_two_it_means():
    assert claims.describe_uses(None) == "nicht zählbar"
    assert claims.describe_uses(0) == "noch nie"
    assert claims.describe_uses(1) == "in 1 Brief"
    assert claims.describe_uses(21) == "in 21 Briefen"


def test_headline_binds_the_fact_to_its_project():
    assert claims.headline({"fact": "Django & DRF", "binding": "Praktikum"}) \
        == "Django & DRF — Praktikum"
    assert claims.headline({"fact": "IHK-Abschluss", "binding": ""}) \
        == "IHK-Abschluss"
    assert claims.headline({"fact": " Java ", "binding": "  "}) == "Java"


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
def test_a_new_claim_sorts_after_the_existing_ones(con):
    first = db.add_claim(con, {"fact": "IHK-Abschluss"})
    second = db.add_claim(con, {"fact": "Django & DRF", "binding": "Praktikum"})
    third = db.add_claim(con, {"fact": "Java & Spring Boot"})

    rows = db.list_claims(con)
    assert [r["id"] for r in rows] == [first, second, third]
    assert [r["sort_order"] for r in rows] == sorted(r["sort_order"] for r in rows)
    assert rows[1]["binding"] == "Praktikum"


def test_claims_of_equal_rank_keep_a_stable_order(con):
    """Two claims placed at the same rank must not swap between renders —
    the register is read as a list, and a list that reshuffles is unreadable."""
    first = db.add_claim(con, {"fact": "A", "sort_order": 5})
    second = db.add_claim(con, {"fact": "B", "sort_order": 5})
    assert [r["id"] for r in db.list_claims(con)] == [first, second]


def test_update_and_delete_a_claim(con):
    claim_id = db.add_claim(con, {"fact": "Djano", "terms": "Djano"})
    db.update_claim(con, claim_id, {
        "fact": "Django & DRF", "binding": "Praktikum bei einer Agentur",
        "terms": "Django, DRF",
    })
    row = db.list_claims(con)[0]
    assert (row["fact"], row["binding"], row["terms"]) == (
        "Django & DRF", "Praktikum bei einer Agentur", "Django, DRF")

    db.delete_claim(con, claim_id)
    assert db.list_claims(con) == []


def test_values_are_stored_stripped(con):
    claim_id = db.add_claim(con, {"fact": "  Java  ", "binding": " Eigenprojekt ",
                                  "terms": " Java , Spring "})
    row = db.list_claims(con)[0]
    assert (row["fact"], row["binding"]) == ("Java", "Eigenprojekt")
    db.update_claim(con, claim_id, {"fact": "  Java 21 ", "binding": " P ",
                                    "terms": " Java "})
    assert db.list_claims(con)[0]["fact"] == "Java 21"


def test_letter_bodies_counts_every_draft_not_only_the_sent_ones(con):
    """A claim written and then discarded is exactly what the register should
    show. Reading only sent letters would report it as never used."""
    _letter(con, "… FastAPI im Abschlussprojekt …", "REF-A")
    job = _letter(con, "… Django im Praktikum …", "REF-B")
    db.upsert_draft(con, job, {"status": "discarded"})
    _letter(con, "   ", "REF-C")  # an empty body claims nothing

    bodies = db.letter_bodies(con)
    assert len(bodies) == 2
    assert claims.count_uses("FastAPI", bodies) == 1
    assert claims.count_uses("Django", bodies) == 1


def test_claims_signature_moves_with_the_register_and_with_the_letters(con):
    before = db.claims_signature(con)

    claim_id = db.add_claim(con, {"fact": "Java"})
    added = db.claims_signature(con)
    assert added != before

    db.update_claim(con, claim_id, {"fact": "Java 21", "terms": "Java"})
    edited = db.claims_signature(con)
    assert edited != added

    _letter(con, "… Java 21 …", "REF-A")
    assert db.claims_signature(con) != edited


def test_editing_a_claim_within_one_second_still_moves_the_signature(con):
    """`updated_at` has second resolution, and correcting the terms then
    watching the counter move is the loop this screen exists for — it happens
    inside one tick. A signature built on the timestamp would tell the watcher
    the screen was already current and freeze the number he is looking at."""
    claim_id = db.add_claim(con, {"fact": "Java", "terms": "Java"})
    before = db.claims_signature(con)
    db.update_claim(con, claim_id, {"fact": "Java", "terms": "Java, Spring"})
    stamps = [r["updated_at"] for r in db.list_claims(con)]
    assert db.claims_signature(con) != before, (
        f"signature blind to an edit within one second (stamps: {stamps})")


def test_reordering_the_register_moves_the_signature(con):
    """Two claims swapping rank changes what the screen reads top to bottom
    while every count, id and timestamp stays exactly the same."""
    first = db.add_claim(con, {"fact": "A", "sort_order": 1})
    db.add_claim(con, {"fact": "B", "sort_order": 2})
    before = db.claims_signature(con)
    con.execute("UPDATE claims SET sort_order=3 WHERE id=?", (first,))
    assert db.claims_signature(con) != before


def test_a_letter_edited_to_new_text_moves_the_signature(con):
    """He can rewrite an Anschreiben by hand in the draft editor, and that
    changes what every counter in the register means."""
    job = _letter(con, "… Django im Praktikum …", "REF-A")
    before = db.claims_signature(con)
    db.upsert_draft(con, job, {"anschreiben_body": "… FastAPI im Projekt …"})
    assert db.claims_signature(con) != before


def test_migrating_an_actual_v8_database_creates_the_register(con):
    """His database is at v8 and carries 60+ applications, so the upgrade has
    to be exercised as an upgrade. The earlier version of this test added a row
    to the already-v9 fixture and re-ran migrate — an idempotency check wearing
    a migration's name, which no `if version < N` step would ever reach."""
    from jobdeck import migrations

    con.execute("DROP TABLE claims")
    con.execute("PRAGMA user_version = 8")
    con.commit()
    assert con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='claims'"
    ).fetchone()[0] == 0

    migrations.migrate(con)

    assert (con.execute("PRAGMA user_version").fetchone()[0]
            == migrations.SCHEMA_VERSION)
    claim_id = db.add_claim(con, {"fact": "IHK-Abschluss"})
    assert [r["id"] for r in db.list_claims(con)] == [claim_id]


def test_re_running_the_migration_never_truncates_the_register(con):
    db.add_claim(con, {"fact": "IHK-Abschluss"})
    con.commit()
    from jobdeck import migrations
    migrations.migrate(con)
    assert [r["fact"] for r in db.list_claims(con)] == ["IHK-Abschluss"]


def test_a_letter_rewritten_to_the_same_length_still_moves_the_signature(con):
    """The docstring leans on the timestamp for exactly this case, and the
    term was deletable with the suite green: the length sum does not move when
    a body is replaced by another of the same size."""
    job = _letter(con, "AAAA", "REF-A")
    before = db.claims_signature(con)
    con.execute("UPDATE drafts SET anschreiben_body='BBBB', "
                "updated_at='2099-01-01T00:00:00' WHERE job_id=?", (job,))
    con.commit()

    assert db.claims_signature(con) != before, (
        "a same-length rewrite is invisible — the timestamp term is not "
        "load-bearing")
