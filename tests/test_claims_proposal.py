"""Filling the permission register from profile.md: propose, confirm, meter."""

import json

import pytest

from jobdeck import claims as claims_lib
from jobdeck import config, db
from jobdeck.ai import claims as ai_claims
from jobdeck.ai import llm
from jobdeck.services import claims as claims_service


def _result(payload: dict, *, cost=0.004) -> llm.LLMResult:
    return llm.LLMResult(text=json.dumps(payload), input_tokens=900,
                         output_tokens=200, cost_usd=cost,
                         model="claude-haiku-4-5")


@pytest.fixture()
def profile_file(data_dir):
    config.PROFILE_PATH.write_text(
        "# Profil\n- FastAPI im IHK-Abschlussprojekt\n- Django im Praktikum\n",
        encoding="utf-8")
    return config.PROFILE_PATH


@pytest.fixture()
def ai_on(con):
    db.set_setting(con, "ai_enabled", "1")
    con.commit()


# --------------------------------------------------------------------------
# The reading itself
# --------------------------------------------------------------------------
def test_the_proposal_keeps_the_pair_the_terms_and_where_it_was_read(
        monkeypatch):
    monkeypatch.setattr(llm, "complete", lambda **kw: _result({"claims": [
        {"kind": "skill", "fact": "FastAPI, PostgreSQL, Alembic",
         "binding": "IHK-Abschlussprojekt", "terms": "FastAPI, Alembic",
         "source_ref": "Technische Kenntnisse"},
    ]}))
    claims, usage = ai_claims.extract_claims("irgendein Profil")
    assert claims == [{"kind": "skill",
                       "fact": "FastAPI, PostgreSQL, Alembic",
                       "binding": "IHK-Abschlussprojekt",
                       "terms": "FastAPI, Alembic",
                       "source_ref": "Technische Kenntnisse"}]
    assert usage.cost_usd == 0.004


def _schema_kinds() -> list[str]:
    return (ai_claims.CLAIMS_SCHEMA["properties"]["claims"]["items"]
            ["properties"]["kind"]["enum"])


def test_the_schema_offers_exactly_the_families_the_register_knows():
    """Two lists of families is two places to add one and forget the other,
    after which the model returns a family the register files as something
    else without anyone noticing."""
    assert _schema_kinds() == list(claims_lib.KINDS)


def test_a_family_the_model_invents_is_filed_not_trusted(monkeypatch):
    """Structured outputs constrain the enum, but the register is the thing
    that has to stay findable: a row filed under a family no screen groups by
    is a row he cannot answer."""
    monkeypatch.setattr(llm, "complete", lambda **kw: _result({"claims": [
        {"kind": "hobby", "fact": "Schach", "binding": "", "terms": "Schach",
         "source_ref": "Stärken"},
    ]}))
    claims, _ = ai_claims.extract_claims("profil")
    assert claims[0]["kind"] == "skill"


def test_a_condition_is_never_bound_to_an_employer(monkeypatch):
    """"Ab sofort verfügbar" bound to an employer reads as a promise made to
    that employer, and the letter would repeat it as one."""
    monkeypatch.setattr(llm, "complete", lambda **kw: _result({"claims": [
        {"kind": "condition", "fact": "Ab sofort verfügbar",
         "binding": "Beispiel GmbH", "terms": "ab sofort",
         "source_ref": "Präferenzen"},
        {"kind": "skill", "fact": "Django", "binding": "Praktikum",
         "terms": "Django", "source_ref": "Technische Kenntnisse"},
    ]}))
    claims, _ = ai_claims.extract_claims("profil")
    assert claims[0]["binding"] == ""
    assert claims[1]["binding"] == "Praktikum", (
        "the rule swallowed a binding that belongs to its fact")


def test_a_runaway_provenance_string_cannot_grow_the_register(monkeypatch):
    monkeypatch.setattr(llm, "complete", lambda **kw: _result({"claims": [
        {"kind": "skill", "fact": "Django", "binding": "", "terms": "Django",
         "source_ref": "x" * 5000},
    ]}))
    claims, _ = ai_claims.extract_claims("profil")
    assert len(claims[0]["source_ref"]) == 120


def test_an_entry_without_a_fact_is_dropped(monkeypatch):
    """An empty permission permits and forbids nothing, and its counter has
    no question to answer."""
    monkeypatch.setattr(llm, "complete", lambda **kw: _result({"claims": [
        {"fact": "  ", "binding": "Praktikum", "terms": "Django"},
        {"fact": "Django", "binding": "Praktikum", "terms": "Django"},
    ]}))
    claims, _ = ai_claims.extract_claims("profil")
    assert [c["fact"] for c in claims] == ["Django"]


def test_a_response_that_is_not_the_agreed_shape_raises_with_its_usage(
        monkeypatch):
    """Those tokens were paid for, so the caller must be able to meter them."""
    monkeypatch.setattr(llm, "complete",
                        lambda **kw: llm.LLMResult(
                            text="not json", input_tokens=10, output_tokens=5,
                            cost_usd=0.001, model="claude-haiku-4-5"))
    with pytest.raises(llm.LLMError) as caught:
        ai_claims.extract_claims("profil")
    assert caught.value.usage is not None
    assert caught.value.usage.cost_usd == 0.001


def test_the_profile_handed_over_is_bounded(monkeypatch):
    """One button must not become a large call because a file grew."""
    seen = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return _result({"claims": []})

    monkeypatch.setattr(llm, "complete", capture)
    ai_claims.extract_claims("x" * (ai_claims.MAX_PROFILE_CHARS + 5000))
    assert len(seen["user_content"]) < ai_claims.MAX_PROFILE_CHARS + 100


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------
async def test_nothing_is_sent_while_the_ai_switch_is_off(con, data_dir,
                                                          profile_file,
                                                          monkeypatch):
    """The master switch's whole promise. `ai_enabled` is off by default."""
    def explode(**kwargs):
        raise AssertionError("the API was called with the AI switch off")

    monkeypatch.setattr(llm, "complete", explode)
    result = await claims_service.import_from_profile()

    assert result["ok"] is False
    assert "ausgeschaltet" in result["error"]
    assert result["written"] == 0


async def test_an_empty_profile_is_refused_before_any_call(con, data_dir, ai_on,
                                                           monkeypatch):
    def explode(**kwargs):
        raise AssertionError("nothing to read, but the API was called")

    monkeypatch.setattr(llm, "complete", explode)
    result = await claims_service.import_from_profile()

    assert result["ok"] is False
    assert "profile.md" in result["error"]


async def test_a_missing_api_key_answers_instead_of_vanishing(
        con, data_dir, ai_on, profile_file, monkeypatch):
    """`LLMNotConfigured` is a SIBLING of `LLMError`, not a subclass, so
    catching LLMError alone let it escape to_thread, past the page's await,
    into NiceGUI's handler wrapper — which turns it into a log line. The
    button then did nothing and said nothing, however often it was pressed.
    He meets this every time he runs against a copied data dir, which is this
    project's own verification recipe."""
    def no_key(**kwargs):
        raise llm.LLMNotConfigured("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(llm, "complete", no_key)
    result = await claims_service.import_from_profile()

    assert result["ok"] is False
    assert "Schlüssel" in result["error"]
    assert result["written"] == 0


async def test_a_second_press_while_one_reading_runs_is_refused(
        con, data_dir, ai_on, profile_file, monkeypatch):
    """The button's only feedback is a toast that fades while the call runs —
    the shape that gets pressed three times and billed three times. Every
    other spending service in this app is single-flight."""
    import asyncio
    import threading

    release = threading.Event()

    def slow(**kwargs):
        release.wait(timeout=5)
        return _result({"claims": []})

    monkeypatch.setattr(llm, "complete", slow)
    first = asyncio.create_task(claims_service.import_from_profile())
    await asyncio.sleep(0.05)

    second = await claims_service.import_from_profile()
    assert second["ok"] is False
    assert "bereits" in second["error"]

    release.set()
    await first


# --------------------------------------------------------------------------
# Reading, and what it costs
# --------------------------------------------------------------------------
async def test_a_reading_writes_proposals_and_confirms_nothing(
        con, data_dir, ai_on, profile_file, monkeypatch):
    """The reading now LANDS in the register — as proposals.

    It used to hand its result to a dialog and keep nothing, so a closed
    window threw away a call he had paid for. What must stay true is the
    other half: none of it counts. A proposal is visible, answerable, and
    invisible to everything that asks what a letter may claim.
    """
    monkeypatch.setattr(llm, "complete", lambda **kw: _result({"claims": [
        {"kind": "skill", "fact": "FastAPI", "binding": "IHK-Projekt",
         "terms": "FastAPI", "source_ref": "Technische Kenntnisse"},
    ]}))
    result = await claims_service.import_from_profile()

    assert result["ok"] is True and result["written"] == 1
    row = db.list_claims(con)[0]
    assert row["state"] == "proposed"
    assert row["source"] == "profile_md"
    assert row["source_ref"] == "Technische Kenntnisse"
    assert row["confirmed_at"] == ""
    assert db.list_claims(con, states=("confirmed",)) == [], (
        "a reading confirmed its own proposal")
    assert db.get_setting(con, "llm_calls", "0") == "1"
    assert float(db.get_setting(con, "llm_cost_usd", "0")) == pytest.approx(0.004)


async def test_a_failed_call_is_still_metered(con, data_dir, ai_on,
                                              profile_file, monkeypatch):
    monkeypatch.setattr(llm, "complete",
                        lambda **kw: llm.LLMResult(
                            text="{}", input_tokens=900, output_tokens=3,
                            cost_usd=0.002, model="claude-haiku-4-5"))
    result = await claims_service.import_from_profile()

    assert result["ok"] is False
    assert float(db.get_setting(con, "llm_cost_usd", "0")) == pytest.approx(0.002)
    assert db.list_claims(con, states=claims_lib.STATES) == []


async def test_what_the_register_already_holds_is_not_read_in_again(
        con, data_dir, ai_on, profile_file, monkeypatch):
    """A second row saying the same thing would split its counter in two."""
    db.add_claim(con, {"fact": "FastAPI", "binding": "IHK-Projekt",
                       "state": "confirmed"})
    con.commit()
    monkeypatch.setattr(llm, "complete", lambda **kw: _result({"claims": [
        {"kind": "skill", "fact": "fastapi", "binding": "  IHK-Projekt ",
         "terms": "FastAPI", "source_ref": "Technische Kenntnisse"},
        {"kind": "skill", "fact": "Django", "binding": "Praktikum",
         "terms": "Django", "source_ref": "Technische Kenntnisse"},
    ]}))
    result = await claims_service.import_from_profile()

    assert result["written"] == 1 and result["skipped"] == 1
    assert [r["fact"] for r in db.list_claims(con)] == ["FastAPI", "Django"]


async def test_a_claim_he_already_refused_is_not_offered_again(
        con, data_dir, ai_on, profile_file, monkeypatch):
    """The reason a refusal is kept rather than deleted. Without it, every
    reading hands him back the same rows he has already said no to, and the
    shelf of proposals never empties."""
    refused = db.add_claim(con, {"fact": "C#", "binding": "Eigenprojekt"})
    db.set_claim_state(con, refused, "rejected")
    con.commit()
    monkeypatch.setattr(llm, "complete", lambda **kw: _result({"claims": [
        {"kind": "skill", "fact": "C#", "binding": "Eigenprojekt",
         "terms": "C#", "source_ref": "Technische Kenntnisse"},
    ]}))
    result = await claims_service.import_from_profile()

    assert result["written"] == 0 and result["skipped"] == 1
    assert db.list_claims(con) == []


async def test_one_competence_at_two_projects_stays_two_permissions(
        con, data_dir, ai_on, profile_file, monkeypatch):
    """Collapsing them is exactly the weld the register exists to prevent."""
    db.add_claim(con, {"fact": "Python", "binding": "Praktikum",
                       "state": "confirmed"})
    con.commit()
    monkeypatch.setattr(llm, "complete", lambda **kw: _result({"claims": [
        {"kind": "skill", "fact": "Python", "binding": "Eigenprojekt",
         "terms": "Python", "source_ref": "Technische Kenntnisse"},
    ]}))
    result = await claims_service.import_from_profile()

    assert result["written"] == 1
    assert [r["binding"] for r in db.list_claims(con)] == [
        "Praktikum", "Eigenprojekt"]


# --------------------------------------------------------------------------
# Answering
# --------------------------------------------------------------------------
async def test_answering_confirms_only_what_he_pointed_at(con, data_dir):
    keep = db.add_claim(con, {"fact": "FastAPI", "binding": "IHK-Projekt",
                              "terms": "FastAPI"})
    leave = db.add_claim(con, {"fact": "Django", "binding": "Praktikum"})
    con.commit()

    changed = await claims_service.answer([keep], "confirmed")

    assert changed == 1
    rows = {r["id"]: r["state"] for r in db.list_claims(con)}
    assert rows[keep] == "confirmed"
    assert rows[leave] == "proposed", "answering one answered another"


async def test_a_whole_family_is_answered_in_one_gesture(con, data_dir):
    """Fifty rows across eight families is why the shelf has a button per
    family. A partial result would leave a heading whose count no longer
    matches what is under it."""
    ids = [db.add_claim(con, {"fact": f"Fach {n}", "binding": "Praktikum",
                              "kind": "experience"}) for n in range(4)]
    con.commit()

    changed = await claims_service.answer(ids, "confirmed")

    assert changed == 4
    assert all(r["state"] == "confirmed" for r in db.list_claims(con))


async def test_answering_an_already_answered_claim_changes_nothing(con,
                                                                   data_dir):
    """Re-confirming would move a confirmation date he never touched."""
    claim_id = db.add_claim(con, {"fact": "FastAPI", "binding": "IHK-Projekt",
                                  "state": "confirmed"})
    con.commit()
    stamped = db.list_claims(con)[0]["confirmed_at"]

    changed = await claims_service.answer([claim_id], "rejected")

    assert changed == 0
    row = db.list_claims(con)[0]
    assert row["state"] == "confirmed" and row["confirmed_at"] == stamped


async def test_a_confirmed_entry_is_immediately_countable(con, data_dir):
    """The point of the terms: the register can measure itself the moment it
    is filled, rather than after the next letter is written."""
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "e1", "title": "t", "company": "c"})
    db.upsert_draft(con, job_id, {"status": "ready",
                                  "anschreiben_body": "… FastAPI …"})
    ids = [db.add_claim(con, {"fact": "FastAPI", "binding": "IHK-Projekt",
                              "terms": "FastAPI"}),
           db.add_claim(con, {"fact": "Java", "binding": "Eigenprojekt",
                              "terms": "Spring Boot"})]
    con.commit()

    await claims_service.answer(ids, "confirmed")

    letters = db.letter_bodies(con)
    counted = {r["fact"]: claims_lib.count_uses(r["terms"], letters)
               for r in db.list_claims(con, states=("confirmed",))}
    assert counted == {"FastAPI": 1, "Java": 0}
