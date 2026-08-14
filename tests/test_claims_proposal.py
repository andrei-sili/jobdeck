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
def test_the_proposal_keeps_the_pair_and_the_terms(monkeypatch):
    monkeypatch.setattr(llm, "complete", lambda **kw: _result({"claims": [
        {"fact": "FastAPI, PostgreSQL, Alembic",
         "binding": "IHK-Abschlussprojekt", "terms": "FastAPI, Alembic"},
    ]}))
    claims, usage = ai_claims.extract_claims("irgendein Profil")
    assert claims == [{"fact": "FastAPI, PostgreSQL, Alembic",
                       "binding": "IHK-Abschlussprojekt",
                       "terms": "FastAPI, Alembic"}]
    assert usage.cost_usd == 0.004


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
