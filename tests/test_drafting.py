import pytest

from jobdeck import config, db
from jobdeck.ai import drafting as ai_drafting
from jobdeck.ai import llm
from jobdeck.services import drafting


# -- betreff (built in code, never by the LLM) --------------------------------
def test_build_betreff_full_and_partial():
    assert ai_drafting.build_betreff(
        "Python Entwickler (m/w/d)", "K-2026-17", "Max Muster"
    ) == "Bewerbung als Python Entwickler (m/w/d), K-2026-17 – Max Muster"
    assert ai_drafting.build_betreff("Dev", "", "Max Muster") \
        == "Bewerbung als Dev – Max Muster"
    assert ai_drafting.build_betreff("Dev", "K-1") == "Bewerbung als Dev, K-1"
    assert ai_drafting.build_betreff(" Dev ") == "Bewerbung als Dev"


def test_resolve_refnr_prefers_extraction_then_arbeitsagentur_id():
    assert drafting.resolve_refnr(
        {"refnr": "K-9", "source": "arbeitsagentur", "external_id": "10001-X"}
    ) == "K-9"
    assert drafting.resolve_refnr(
        {"refnr": "", "source": "arbeitsagentur", "external_id": "10001-X"}
    ) == "10001-X"
    assert drafting.resolve_refnr(
        {"refnr": "", "source": "jooble", "external_id": "12345"}
    ) == ""


# -- drafting module -----------------------------------------------------------
def _job(**over):
    values = dict(
        title="Python Developer", company="Firma GmbH", location="Berlin",
        remote=0, description="Python, FastAPI, pytest", refnr="K-17",
        ansprechpartner="Frau Weber",
    )
    values.update(over)
    return values


def test_build_user_content_fences_posting_and_names_contact():
    job = _job(description="x" * (ai_drafting.MAX_DESCRIPTION_CHARS + 100))
    content = ai_drafting.build_user_content(
        job, "my profile", refnr="K-17", applicant_name="Erika Muster"
    )
    assert "my profile" in content
    assert "Name: Erika Muster" in content
    assert "Referenznummer: K-17" in content  # the resolved one, as in the Betreff
    assert "Ansprechpartner: Frau Weber" in content
    assert content.count("x") == ai_drafting.MAX_DESCRIPTION_CHARS
    assert content.index("<<<POSTING START>>>") < content.index("x" * 10)
    assert content.rstrip().endswith("<<<POSTING END>>>")


def test_build_betreff_collapses_smuggled_whitespace():
    # posting-derived title must not inject newlines into a subject line
    assert ai_drafting.build_betreff("Dev\nX-Evil: 1", "K\n1", "Max\tMuster") \
        == "Bewerbung als Dev X-Evil: 1, K 1 – Max Muster"


def test_draft_application_parses_and_strips(monkeypatch):
    def fake_complete(**kwargs):
        return llm.LLMResult(
            text='{"anschreiben_body": " Sehr geehrte Frau Weber,\\n\\nAbsatz. ",'
                 ' "email_body": " Guten Tag,\\n\\nanbei meine Bewerbung. "}',
            model="m", input_tokens=5, output_tokens=5, cost_usd=0.0,
        )

    monkeypatch.setattr(llm, "complete", fake_complete)
    anschreiben, email_body, usage = ai_drafting.draft_application(_job(), "profil")
    assert anschreiben.startswith("Sehr geehrte Frau Weber,")
    assert email_body.endswith("anbei meine Bewerbung.")
    assert usage.input_tokens == 5


@pytest.mark.parametrize("text", [
    "not json",
    '{"anschreiben_body": "", "email_body": "x"}',   # empty text is unusable
])
def test_draft_application_rejects_unusable_response(monkeypatch, text):
    def fake_complete(**kwargs):
        return llm.LLMResult(
            text=text, model="m", input_tokens=1, output_tokens=1, cost_usd=0.001,
        )

    monkeypatch.setattr(llm, "complete", fake_complete)
    with pytest.raises(llm.LLMError) as excinfo:
        ai_drafting.draft_application(_job(), "profil")
    assert excinfo.value.usage is not None  # billed call stays meterable


# -- drafting service ----------------------------------------------------------
def _insert_job(con, **over):
    values = dict(
        source="stub", external_id=over.pop("external_id", "j1"),
        title="Python Dev", company="Firma", description="desc",
        contact_email="hr@firma.de",
    )
    values.update(over)
    return db.insert_job_if_new(con, values)


def _usage(cost=0.002):
    return llm.LLMResult(
        text="", model="claude-haiku-4-5",
        input_tokens=100, output_tokens=200, cost_usd=cost,
    )


@pytest.fixture()
def ai_on(con):
    db.set_setting(con, "ai_enabled", "1")
    con.commit()


@pytest.fixture()
def applicant(con):
    db.set_setting(con, "applicant_name", "Max Muster")
    con.commit()


@pytest.fixture()
def profile_file(data_dir):
    config.PROFILE_PATH.write_text("Python developer, 3 years", encoding="utf-8")


def _must_not_be_called(job, profile_text, refnr="", applicant_name=""):
    raise AssertionError("LLM called although a gate should have fired")


async def test_gates_fire_in_order_without_spend(con, monkeypatch):
    monkeypatch.setattr("jobdeck.ai.drafting.draft_application", _must_not_be_called)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()

    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and "AI is disabled" in result["error"]

    db.set_setting(con, "ai_enabled", "1")
    con.commit()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and "ANTHROPIC_API_KEY" in result["error"]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    result = await drafting.draft_for_job(job_id)  # no profile.md yet
    assert not result["ok"] and "profile" in result["error"]

    config.PROFILE_PATH.write_text("profil", encoding="utf-8")
    result = await drafting.draft_for_job(job_id)  # no applicant name yet
    assert not result["ok"] and "applicant name" in result["error"]

    assert db.get_setting(con, "llm_calls", "0") == "0"
    assert db.get_draft_by_job(con, job_id) is None


async def test_successful_draft_is_persisted_and_metered(
    con, ai_on, applicant, profile_file, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    db.set_job_contacts(  # the real flow: extraction filled these
        con, job_id, {"refnr": "K-17", "ansprechpartner": "Frau Weber"}
    )
    con.commit()

    monkeypatch.setattr(
        "jobdeck.ai.drafting.draft_application",
        lambda job, profile_text, refnr="", applicant_name="":
            ("Sehr geehrte Frau Weber,\n\nAbsatz.",
                                   "Guten Tag,\n\nanbei meine Bewerbung.\n\n"
                                   "Mit freundlichen Grüßen\nMax Muster",
                                   _usage()),
    )

    result = await drafting.draft_for_job(job_id)
    assert result["ok"], result["error"]
    draft = result["draft"]
    assert draft["status"] == "ready"
    assert draft["betreff"] == "Bewerbung als Python Dev, K-17 – Max Muster"
    assert draft["recipient"] == "hr@firma.de"
    assert draft["anschreiben_body"].startswith("Sehr geehrte Frau Weber,")
    assert draft["llm_model"] == "claude-haiku-4-5"

    assert db.get_setting(con, "llm_calls") == "1"
    assert float(db.get_setting(con, "llm_cost_usd")) == pytest.approx(0.002)
    assert db.get_draft_by_job(con, job_id)["status"] == "ready"


async def test_failed_draft_is_recorded_and_metered(
    con, ai_on, applicant, profile_file, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()

    def failing(job, profile_text, refnr="", applicant_name=""):
        raise llm.LLMError("unparseable", usage=_usage(cost=0.003))

    monkeypatch.setattr("jobdeck.ai.drafting.draft_application", failing)

    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and "drafting failed" in result["error"]
    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == "failed"
    assert "unparseable" in draft["error"]
    assert float(db.get_setting(con, "llm_cost_usd")) == pytest.approx(0.003)

    # a failed draft is re-claimable immediately — the user may retry
    monkeypatch.setattr(
        "jobdeck.ai.drafting.draft_application",
        lambda job, profile_text, refnr="", applicant_name="":
            ("Anrede,\n\nText.", "Mail.", _usage()),
    )
    result = await drafting.draft_for_job(job_id)
    assert result["ok"]
    assert db.get_draft_by_job(con, job_id)["status"] == "ready"


async def test_redraft_clears_stale_pdf_path(
    con, ai_on, applicant, profile_file, monkeypatch
):
    """A regenerated draft must not keep pointing at the OLD letter's PDF."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()
    monkeypatch.setattr(
        "jobdeck.ai.drafting.draft_application",
        lambda job, profile_text, refnr="", applicant_name="":
            ("Anrede,\n\nText.", "Mail.", _usage()),
    )
    assert (await drafting.draft_for_job(job_id))["ok"]
    db.upsert_draft(con, job_id, {"pdf_path": "/old/mappe.pdf"})
    con.commit()

    result = await drafting.draft_for_job(job_id)  # re-draft
    assert result["ok"]
    assert result["draft"]["pdf_path"] == ""


async def test_generating_claim_blocks_double_spend(
    con, ai_on, applicant, profile_file, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()
    db.upsert_draft(con, job_id, {"status": "generating"})
    con.commit()

    monkeypatch.setattr("jobdeck.ai.drafting.draft_application", _must_not_be_called)
    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and "already being generated" in result["error"]
    assert db.get_setting(con, "llm_calls", "0") == "0"


async def test_llm_not_configured_releases_claim_without_metering(
    con, ai_on, applicant, profile_file, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()

    def not_configured(job, profile_text, refnr="", applicant_name=""):
        raise llm.LLMNotConfigured("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr("jobdeck.ai.drafting.draft_application", not_configured)
    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and "ANTHROPIC_API_KEY" in result["error"]
    assert db.get_draft_by_job(con, job_id)["status"] == "failed"
    assert db.get_setting(con, "llm_calls", "0") == "0"  # nothing was billed


async def test_recent_claim_still_blocks_just_under_the_timeout(
    con, ai_on, applicant, profile_file, monkeypatch
):
    import datetime

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()
    db.upsert_draft(con, job_id, {"status": "generating"})
    recent = (datetime.datetime.now()
              - datetime.timedelta(minutes=drafting.CLAIM_TIMEOUT_MIN - 1)
              ).isoformat(timespec="seconds")
    con.execute("UPDATE drafts SET updated_at=?", (recent,))
    con.commit()

    monkeypatch.setattr("jobdeck.ai.drafting.draft_application", _must_not_be_called)
    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and "already being generated" in result["error"]


@pytest.mark.parametrize("status,hint", [
    ("approved", "return it to ready"),
    ("sending", "resolve it"),
    ("sent", "already sent"),
])
async def test_send_path_drafts_are_never_regenerated(
    con, ai_on, applicant, profile_file, monkeypatch, status, hint
):
    """A draft committed to the send path must survive a Draft click.

    Stealing a 'sending' claim would destroy the stuck-send evidence the
    review queue needs and open a double-send to the company."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()
    db.upsert_draft(con, job_id, {
        "status": status, "anschreiben_body": "Sehr geehrte Damen und Herren,",
        "pdf_path": "/tmp/mappe.pdf",
    })
    stale = "2020-01-01T00:00:00"  # older than CLAIM_TIMEOUT_MIN: age must not matter
    con.execute("UPDATE drafts SET updated_at=?", (stale,))
    con.commit()

    monkeypatch.setattr("jobdeck.ai.drafting.draft_application", _must_not_be_called)
    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and hint in result["error"]

    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == status  # untouched
    assert draft["pdf_path"] == "/tmp/mappe.pdf"  # not wiped
    assert draft["anschreiben_body"] == "Sehr geehrte Damen und Herren,"
    assert db.get_setting(con, "llm_calls", "0") == "0"


async def test_finish_discards_result_when_claim_was_taken_away(
    con, ai_on, applicant, profile_file, monkeypatch
):
    """A draft resolved/discarded mid-generation must not be overwritten."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()

    def steal_then_draft(job, profile_text, refnr="", applicant_name=""):
        # simulates a human resolving the draft while the LLM call runs
        with db.db() as other:
            db.upsert_draft(other, job_id, {"status": "discarded"})
        return ("Anrede,\n\nText.", "Mail.", _usage())

    monkeypatch.setattr("jobdeck.ai.drafting.draft_application", steal_then_draft)
    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and "changed while" in result["error"]

    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == "discarded"  # the newer state wins
    assert draft["email_body"] == ""
    assert db.get_setting(con, "llm_calls") == "1"  # the call was still billed


async def test_abandoned_claim_is_reclaimed(
    con, ai_on, applicant, profile_file, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()
    db.upsert_draft(con, job_id, {"status": "generating"})
    stale = "2020-01-01T00:00:00"  # far older than CLAIM_TIMEOUT_MIN
    con.execute("UPDATE drafts SET updated_at=?", (stale,))
    con.commit()

    monkeypatch.setattr(
        "jobdeck.ai.drafting.draft_application",
        lambda job, profile_text, refnr="", applicant_name="":
            ("Anrede,\n\nText.", "Mail.", _usage()),
    )
    result = await drafting.draft_for_job(job_id)
    assert result["ok"]
    assert db.get_draft_by_job(con, job_id)["status"] == "ready"
