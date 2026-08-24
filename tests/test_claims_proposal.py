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
    result = await claims_service.propose_from_profile()

    assert result["ok"] is False
    assert "ausgeschaltet" in result["error"]
    assert result["claims"] == []


async def test_an_empty_profile_is_refused_before_any_call(con, data_dir, ai_on,
                                                           monkeypatch):
    def explode(**kwargs):
        raise AssertionError("nothing to read, but the API was called")

    monkeypatch.setattr(llm, "complete", explode)
    result = await claims_service.propose_from_profile()

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
    result = await claims_service.propose_from_profile()

    assert result["ok"] is False
    assert "Schlüssel" in result["error"]
    assert result["claims"] == []


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
    first = asyncio.create_task(claims_service.propose_from_profile())
    await asyncio.sleep(0.05)

    second = await claims_service.propose_from_profile()
    assert second["ok"] is False
    assert "bereits" in second["error"]

    release.set()
    await first


# --------------------------------------------------------------------------
# Proposing, and what it costs
# --------------------------------------------------------------------------
async def test_a_proposal_writes_nothing_but_the_meter(con, data_dir, ai_on,
                                                       profile_file,
                                                       monkeypatch):
    monkeypatch.setattr(llm, "complete", lambda **kw: _result({"claims": [
        {"fact": "FastAPI", "binding": "IHK-Projekt", "terms": "FastAPI"},
    ]}))
    result = await claims_service.propose_from_profile()

    assert result["ok"] is True
    assert [c["fact"] for c in result["claims"]] == ["FastAPI"]
    assert result["claims"][0]["headline"] == "FastAPI — IHK-Projekt"
    assert db.list_claims(con) == [], "a proposal reached the register"
    assert db.get_setting(con, "llm_calls", "0") == "1"
    assert float(db.get_setting(con, "llm_cost_usd", "0")) == pytest.approx(0.004)


async def test_a_failed_call_is_still_metered(con, data_dir, ai_on,
                                              profile_file, monkeypatch):
    monkeypatch.setattr(llm, "complete",
                        lambda **kw: llm.LLMResult(
                            text="{}", input_tokens=900, output_tokens=3,
                            cost_usd=0.002, model="claude-haiku-4-5"))
    result = await claims_service.propose_from_profile()

    assert result["ok"] is False
    assert float(db.get_setting(con, "llm_cost_usd", "0")) == pytest.approx(0.002)


async def test_what_the_register_already_holds_is_not_proposed_again(
        con, data_dir, ai_on, profile_file, monkeypatch):
    """A second row saying the same thing would split its counter in two."""
    db.add_claim(con, {"fact": "FastAPI", "binding": "IHK-Projekt"})
    con.commit()
    monkeypatch.setattr(llm, "complete", lambda **kw: _result({"claims": [
        {"fact": "fastapi", "binding": "  IHK-Projekt ", "terms": "FastAPI"},
        {"fact": "Django", "binding": "Praktikum", "terms": "Django"},
    ]}))
    result = await claims_service.propose_from_profile()

    assert [c["fact"] for c in result["claims"]] == ["Django"]
    assert result["skipped"] == 1


async def test_one_competence_at_two_projects_stays_two_permissions(
        con, data_dir, ai_on, profile_file, monkeypatch):
    """Collapsing them is exactly the weld the register exists to prevent."""
    db.add_claim(con, {"fact": "Python", "binding": "Praktikum"})
    con.commit()
    monkeypatch.setattr(llm, "complete", lambda **kw: _result({"claims": [
        {"fact": "Python", "binding": "Eigenprojekt", "terms": "Python"},
    ]}))
    result = await claims_service.propose_from_profile()

    assert [c["binding"] for c in result["claims"]] == ["Eigenprojekt"]
    assert result["skipped"] == 0


# --------------------------------------------------------------------------
# Confirming
# --------------------------------------------------------------------------
async def test_only_what_he_keeps_is_written(con, data_dir):
    written = await claims_service.accept([
        {"fact": "FastAPI", "binding": "IHK-Projekt", "terms": "FastAPI"},
    ])
    assert written == 1
    rows = db.list_claims(con)
    assert [(r["fact"], r["terms"]) for r in rows] == [("FastAPI", "FastAPI")]


async def test_accepting_re_checks_the_register_as_it_is_now(con, data_dir):
    """The proposal was made before he read it; he may have typed one of these
    in by hand in the meantime, in another tab."""
    proposal = [{"fact": "FastAPI", "binding": "IHK-Projekt", "terms": "FastAPI"},
                {"fact": "Django", "binding": "Praktikum", "terms": "Django"}]
    db.add_claim(con, {"fact": "FastAPI", "binding": "IHK-Projekt"})
    con.commit()

    written = await claims_service.accept(proposal)

    assert written == 1
    assert [r["fact"] for r in db.list_claims(con)] == ["FastAPI", "Django"]


async def test_the_stored_entries_are_immediately_countable(con, data_dir):
    """The point of the terms: the register can measure itself the moment it
    is filled, rather than after the next letter is written."""
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "e1", "title": "t", "company": "c"})
    db.upsert_draft(con, job_id, {"status": "ready",
                                  "anschreiben_body": "… FastAPI …"})
    con.commit()

    await claims_service.accept([
        {"fact": "FastAPI", "binding": "IHK-Projekt", "terms": "FastAPI"},
        {"fact": "Java", "binding": "Eigenprojekt", "terms": "Spring Boot"},
    ])

    letters = db.letter_bodies(con)
    counted = {r["fact"]: claims_lib.count_uses(r["terms"], letters)
               for r in db.list_claims(con)}
    assert counted == {"FastAPI": 1, "Java": 0}
