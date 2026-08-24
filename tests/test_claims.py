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


# ---------------------------------------------------------------------------
# The families and the verification state (schema v15)
# ---------------------------------------------------------------------------
def test_an_unreadable_state_is_never_read_as_confirmed():
    """`confirmed` is the value that lets a fact into a letter.

    Anything the column cannot be read as — an empty string, a typo, a state
    from a future version rolled back onto this one — has to land on the
    unverified side. Defaulting the other way would let a fact nobody vouched
    for be claimed, which is the one thing this column exists to stop.
    """
    for raw in ("", None, "bestätigt", "CONFIRMED!", "verified", 7, "  "):
        assert claims.normalise_state(raw) == "proposed"
    for state in claims.STATES:
        assert claims.normalise_state(state) == state
    assert claims.normalise_state("  Confirmed ") == "confirmed"


def test_an_unreadable_family_lands_on_the_strictest_one():
    """Unknown becomes `skill` — what every row meant before v15, and the
    family whose rule is tightest. Filing too strictly is one edit away;
    filing too loosely is the weld the register exists to prevent."""
    for raw in ("", None, "sprache", "Fähigkeit", 3):
        assert claims.normalise_kind(raw) == "skill"
    for kind in claims.KINDS:
        assert claims.normalise_kind(kind.upper()) == kind


def test_every_family_has_a_german_name_and_one_fixed_place():
    """The screen groups by family, so the order has to be total and stable —
    two renders that disagree would move a row under the reader."""
    assert claims.DEFAULT_KIND in claims.KINDS
    assert all(label.strip() for label in claims.KINDS.values())
    assert len(set(claims.KINDS.values())) == len(claims.KINDS)
    places = [claims.kind_order(kind) for kind in claims.KINDS]
    assert places == sorted(places) == list(range(len(claims.KINDS)))
    assert claims.kind_order("nonsense") == claims.kind_order("skill")
    assert claims.kind_label("credential") == "Zertifikate"


def test_an_answered_claim_stays_out_of_the_working_register():
    """`rejected` and `superseded` are answers already given. Showing them in
    the register would offer back exactly what he already refused or
    replaced — and both are kept rather than deleted so a second import
    cannot propose them again."""
    assert set(claims.VISIBLE_STATES) == {"proposed", "confirmed"}
    assert set(claims.VISIBLE_STATES) < set(claims.STATES)
    assert "rejected" not in claims.VISIBLE_STATES
    assert "superseded" not in claims.VISIBLE_STATES


# ---------------------------------------------------------------------------
# Storage: proposals, confirmation, correction (schema v15)
# ---------------------------------------------------------------------------
def _ids(rows):
    return [row["id"] for row in rows]


def test_a_claim_arrives_unverified_unless_the_caller_has_his_word(con):
    """The dangerous direction is a reading that confirms itself.

    Every path into the register except his own form is something READING
    something — his profile today, a document tomorrow. If the repository
    defaulted to `confirmed`, each new one of those would only have to forget
    a keyword to put a fact nobody vouched for into a letter.
    """
    read = db.add_claim(con, {"fact": "FastAPI", "binding": "Abschlussprojekt"})
    typed = db.add_claim(con, {"fact": "Django", "binding": "Praktikum",
                               "state": "confirmed"})
    con.commit()

    rows = {row["id"]: row for row in db.list_claims(con)}
    assert rows[read]["state"] == "proposed"
    assert rows[read]["confirmed_at"] == "", (
        "an unanswered claim carries a confirmation date")
    assert rows[typed]["state"] == "confirmed"
    assert rows[typed]["confirmed_at"], "his own entry was not stamped"


def test_correcting_a_confirmed_claim_keeps_what_older_letters_could_say(con):
    """A correction is a new row; the old one is kept as `superseded`.

    Letters already written were allowed to say what the old row said. A
    register that edited itself in place would make yesterday's letter look
    like it broke a rule that did not exist when it was written.
    """
    original = db.add_claim(con, {
        "fact": "Java", "binding": "Praktikum", "terms": "Java",
        "kind": "skill", "state": "confirmed", "source": "profile_md",
        "source_ref": "Technische Kenntnisse"})
    con.commit()

    corrected = db.update_claim(con, original, {
        "fact": "Java & Spring Boot", "binding": "Eigenprojekt",
        "terms": "Java, Spring Boot"})
    con.commit()

    assert corrected != original, "the correction overwrote the old row"
    old = con.execute("SELECT * FROM claims WHERE id=?", (original,)).fetchone()
    new = con.execute("SELECT * FROM claims WHERE id=?", (corrected,)).fetchone()
    assert old["state"] == "superseded"
    assert old["fact"] == "Java" and old["binding"] == "Praktikum"
    assert new["state"] == "confirmed" and new["supersedes_id"] == original
    assert new["binding"] == "Eigenprojekt"
    assert new["sort_order"] == old["sort_order"], (
        "the correction jumped to the end of the register")
    # Provenance: the wording is his now, but where it was first read is not
    # changed by his editing the sentence.
    assert new["source"] == "user"
    assert new["source_ref"] == "Technische Kenntnisse"
    assert _ids(db.list_claims(con)) == [corrected], (
        "the replaced row is still in the working register")


def test_correcting_a_proposal_edits_it_in_place(con):
    """A proposal is not history — nobody ever vouched for it. Superseding
    proposals would bury the corrections that do matter under rows that were
    never permissions."""
    claim_id = db.add_claim(con, {"fact": "Rdis", "binding": "JobDeck"})
    con.commit()

    same = db.update_claim(con, claim_id, {"fact": "Redis",
                                           "binding": "JobDeck"})
    con.commit()

    assert same == claim_id
    rows = db.list_claims(con)
    assert len(rows) == 1 and rows[0]["fact"] == "Redis"
    assert rows[0]["state"] == "proposed", "editing a proposal confirmed it"
    assert con.execute(
        "SELECT COUNT(*) FROM claims WHERE state='superseded'").fetchone()[0] == 0


def test_only_a_waiting_claim_can_be_answered(con):
    """Re-confirming a confirmed row would move its date without him touching
    it, and `superseded` is written by a correction, never by a button."""
    claim_id = db.add_claim(con, {"fact": "Docker", "binding": "JobDeck",
                                  "state": "confirmed"})
    con.commit()
    stamped = con.execute("SELECT confirmed_at FROM claims WHERE id=?",
                          (claim_id,)).fetchone()[0]

    db.answer_claims(con, [claim_id], "rejected")
    con.commit()

    row = con.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
    assert row["state"] == "confirmed", "an answered claim was answered again"
    assert row["confirmed_at"] == stamped
    for refused in ("superseded", "proposed", "nonsense"):
        with pytest.raises(ValueError):
            db.answer_claims(con, [claim_id], refused)


def test_answering_a_proposal_records_which_answer_it_was(con):
    keep = db.add_claim(con, {"fact": "Kubernetes", "binding": "Eigenprojekt"})
    drop = db.add_claim(con, {"fact": "C#", "binding": ""})
    con.commit()

    db.answer_claims(con, [keep], "confirmed")
    db.answer_claims(con, [drop], "rejected")
    con.commit()

    rows = {row["id"]: row for row in db.list_claims(con, states=claims.STATES)}
    assert rows[keep]["state"] == "confirmed" and rows[keep]["confirmed_at"]
    assert rows[drop]["state"] == "rejected"
    assert rows[drop]["confirmed_at"] == "", (
        "a refusal was stamped as a confirmation")
    assert _ids(db.list_claims(con)) == [keep], (
        "a refused claim is still offered in the register")


def test_the_register_shows_the_open_questions_and_hides_the_answered(con):
    """`rejected` and `superseded` are answers already given. They are kept
    rather than deleted so a second reading of the profile cannot offer back
    what he has already refused — which only works if the caller that asks
    "is this known" sees them."""
    proposed = db.add_claim(con, {"fact": "Go", "binding": ""})
    confirmed = db.add_claim(con, {"fact": "Python", "binding": "",
                                   "state": "confirmed"})
    rejected = db.add_claim(con, {"fact": "C++", "binding": ""})
    con.commit()
    db.answer_claims(con, [rejected], "rejected")
    con.commit()

    assert _ids(db.list_claims(con)) == [proposed, confirmed]
    assert _ids(db.list_claims(con, states=claims.STATES)) == [
        proposed, confirmed, rejected]
    assert _ids(db.list_claims(con, states=("rejected",))) == [rejected]
    assert db.list_claims(con, states=()) == []


def test_the_screen_signature_sees_a_proposal_being_answered(con):
    """The state is the only thing an answer changes.

    A signature blind to it would leave the shelf of waiting proposals on
    screen after he had emptied it — the failure this app has already had to
    fix once, when a loader handed the watcher fewer facts than the watcher
    compared.
    """
    claim_id = db.add_claim(con, {"fact": "Terraform", "binding": ""})
    con.commit()
    before = db.claims_signature(con)

    db.answer_claims(con, [claim_id], "confirmed")
    con.commit()

    assert db.claims_signature(con) != before


def test_the_screen_signature_sees_a_claim_change_family(con):
    """On a PROPOSAL, deliberately. Correcting a confirmed claim inserts a
    row, so the count alone would move the signature and the test would pass
    with the family left out of it entirely. A proposal is edited in place:
    the family is then the only thing that changed."""
    claim_id = db.add_claim(con, {"fact": "Englisch B2", "binding": "",
                                  "kind": "skill"})
    con.commit()
    before = db.claims_signature(con)
    rows_before = con.execute("SELECT COUNT(*) FROM claims").fetchone()[0]

    db.update_claim(con, claim_id, {"fact": "Englisch B2", "binding": "",
                                    "kind": "language"})
    con.commit()

    assert con.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == (
        rows_before), "the correction inserted a row; the count moved, not the family"
    assert db.claims_signature(con) != before


# ---------------------------------------------------------------------------
# What the register does not yet hold
# ---------------------------------------------------------------------------
PROFILE = """\
# Profil — Beispiel
## Basisdaten
Wohnort, Telefon
## Technische Kenntnisse
Python, Django
### Vertiefung
Mehr davon
## Zertifikate
Ein Zertifikat
"""


def test_the_sections_are_read_in_the_order_he_wrote_them():
    assert claims.profile_sections(PROFILE) == [
        "Basisdaten", "Technische Kenntnisse", "Vertiefung", "Zertifikate"]


def test_the_files_own_title_is_not_a_section():
    """A single '#' names the document — his reads "Profil — <sein Name>".
    Counting it made the measurement list his own name among the things
    nothing stands for, and made it one that could never be complete."""
    assert claims.profile_sections("# Profil — Beispiel\n## Basisdaten\n") == [
        "Basisdaten"]


def test_two_sections_with_one_name_collapse():
    """A provenance string carries the NAME, so two sections sharing one
    cannot be told apart by it — counting them separately would report a
    coverage gap nothing could ever close."""
    assert claims.profile_sections(
        "## Zertifikate\na\n## zertifikate\nb\n") == ["Zertifikate"]


def test_a_file_without_headings_has_nothing_to_measure():
    assert claims.profile_sections("nur Fließtext\nund noch eine Zeile") == []
    assert claims.profile_sections("") == []


def _row(source_ref, state="confirmed"):
    return {"source_ref": source_ref, "state": state}


def test_only_a_confirmed_fact_stands_for_a_section():
    """A proposal standing for a section would make the register look ready
    the moment it was read — the one moment nobody has checked it."""
    sections = claims.profile_sections(PROFILE)
    view = claims.coverage(sections, [
        _row("Technische Kenntnisse"),
        _row("Zertifikate", state="proposed"),
        _row("Basisdaten", state="rejected"),
    ])
    assert view["sections"] == 4
    assert view["covered"] == 1
    assert view["missing"] == ["Basisdaten", "Vertiefung", "Zertifikate"]


def test_a_section_is_matched_the_way_the_register_folds_everything_else():
    view = claims.coverage(["Technische Kenntnisse"],
                           [_row("  technische   kenntnisse ")])
    assert view["missing"] == []
    assert view["covered"] == 1


def test_a_fact_that_points_nowhere_covers_nothing():
    """A hand-typed claim has no section to point at, and a provenance that
    names a section the file no longer has points at nothing either. Neither
    may quietly stand for the first section, or for any section at all."""
    view = claims.coverage(["Basisdaten", "Zertifikate"],
                           [_row(""), _row("Ein alter Abschnitt")])
    assert view["covered"] == 0
    assert view["missing"] == ["Basisdaten", "Zertifikate"]


def test_a_bulk_answer_with_nothing_to_answer_writes_nothing(con):
    """The early return is what keeps an empty family button from building
    "id IN ()", which sqlite refuses outright."""
    db.add_claim(con, {"fact": "Django", "binding": ""})
    con.commit()

    assert db.answer_claims(con, [], "confirmed") == 0
    assert db.list_claims(con)[0]["state"] == "proposed"


def test_an_unreadable_profile_reads_as_empty_not_as_a_crash(data_dir):
    """The Unterlagen screen measures this file on every render. A directory
    left in its place — or a mode that stops it being read — would blank a
    whole page rather than be reported, which is the shape that once took
    every screen down at once."""
    from jobdeck import config
    from jobdeck.ai import profile as ai_profile

    config.PROFILE_PATH.mkdir(parents=True, exist_ok=True)
    assert ai_profile.load_profile() == ""
    assert claims.profile_sections(ai_profile.load_profile()) == []


def test_a_hand_typed_claim_says_so_and_an_imported_one_names_its_section():
    """"Who said so" is the question the register has to answer about
    anything it holds, and the two answers must not read alike."""
    assert claims.provenance(
        {"source": "user", "source_ref": ""}) == "von dir eingetragen"
    assert claims.provenance(
        {"source": "profile_md", "source_ref": "Zertifikate"}
    ) == "aus profile.md · Zertifikate"
    # A reading that lost its section still says it was a reading.
    assert claims.provenance(
        {"source": "profile_md", "source_ref": "  "}) == "aus profile.md"
    # An unknown source is not silently promoted to "he said it": that
    # sentence claims HE vouched for the row, and the one thing an unreadable
    # value must never do is vouch.
    assert claims.provenance(
        {"source": "", "source_ref": "Zertifikate"}) == "Herkunft unbekannt"
    assert claims.provenance(
        {"source": "irgendwas", "source_ref": ""}) == "Herkunft unbekannt"


def test_the_grouping_keeps_the_family_order_and_drops_empty_families():
    rows = [{"kind": "condition", "fact": "a"}, {"kind": "skill", "fact": "b"},
            {"kind": "condition", "fact": "c"}, {"kind": "nonsense",
                                                 "fact": "d"}]
    grouped = claims.group_by_kind(rows)

    assert [kind for kind, _label, _rows in grouped] == ["skill", "condition"]
    assert [label for _kind, label, _rows in grouped] == [
        "Technische Kenntnisse", "Rahmenbedingungen"]
    # Rows keep the register's own order beneath their heading, and the
    # unreadable family joined the strictest one rather than vanishing.
    assert [r["fact"] for r in grouped[0][2]] == ["b", "d"]
    assert [r["fact"] for r in grouped[1][2]] == ["a", "c"]
    assert claims.group_by_kind([]) == []


def test_one_proposal_does_not_read_as_several():
    """German inflects; a screen that does not is a screen that looks
    machine-written."""
    assert claims.count_proposals(1) == "1 Vorschlag"
    assert claims.count_proposals(2) == "2 Vorschläge"
    assert claims.count_proposals(0) == "0 Vorschläge"


def test_only_a_refusal_can_be_taken_back(con):
    """A superseded row is the RECORD of a correction: putting it back would
    resurrect wording he replaced, beside the wording that replaced it."""
    refused = db.add_claim(con, {"fact": "C#", "binding": ""})
    con.commit()
    db.answer_claims(con, [refused], "rejected")
    original = db.add_claim(con, {"fact": "Java", "binding": "Praktikum",
                                  "state": "confirmed"})
    con.commit()
    db.update_claim(con, original, {"fact": "Java & Spring", "binding": ""})
    confirmed = db.add_claim(con, {"fact": "Go", "binding": "",
                                   "state": "confirmed"})
    con.commit()

    assert db.restore_claim(con, refused) == 1
    assert db.restore_claim(con, original) == 0, "a superseded row came back"
    assert db.restore_claim(con, confirmed) == 0
    con.commit()

    rows = {r["id"]: r for r in db.list_claims(con, states=claims.STATES)}
    assert rows[refused]["state"] == "proposed"
    assert rows[refused]["confirmed_at"] == "", (
        "a restored claim kept a confirmation it never had")
    assert rows[original]["state"] == "superseded"
