from types import SimpleNamespace

import pytest

from jobdeck.ai import llm, profile, scoring


def _response(
    text,
    input_tokens=100,
    output_tokens=50,
    model="claude-haiku-4-5",
    stop_reason="end_turn",
):
    return SimpleNamespace(
        model=model,
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class StubClient:
    def __init__(self, response):
        self._response = response
        self.kwargs = None
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.kwargs = kwargs
        return self._response


def _job(**over):
    values = dict(
        title="Python Developer",
        company="Firma GmbH",
        location="Berlin",
        remote=0,
        description="Python, FastAPI, pytest",
    )
    values.update(over)
    return values


# -- llm ---------------------------------------------------------------------
def test_pricing_matches_by_prefix_and_defaults_to_zero():
    assert llm.pricing("claude-haiku-4-5") == (1.00, 5.00)
    assert llm.pricing("claude-haiku-4-5-20251001") == (1.00, 5.00)
    assert llm.pricing("some-unknown-model") == (0.0, 0.0)


def test_complete_returns_text_usage_and_cost(monkeypatch):
    stub = StubClient(_response('{"score": 70, "reason": "Passt."}'))
    monkeypatch.setattr(llm, "client", lambda: stub)

    result = llm.complete(
        system="mysystem", user_content="mycontent", max_tokens=300,
        output_schema=scoring.SCORE_SCHEMA,
    )

    assert result.text == '{"score": 70, "reason": "Passt."}'
    assert (result.input_tokens, result.output_tokens) == (100, 50)
    assert result.cost_usd == pytest.approx((100 * 1.0 + 50 * 5.0) / 1_000_000)
    # the request must carry exactly what the caller asked for
    from jobdeck import config

    assert stub.kwargs["model"] == config.anthropic_model()
    assert stub.kwargs["max_tokens"] == 300
    assert stub.kwargs["system"] == "mysystem"
    assert stub.kwargs["messages"] == [{"role": "user", "content": "mycontent"}]
    schema = stub.kwargs["output_config"]["format"]["schema"]
    assert schema == scoring.SCORE_SCHEMA


def test_complete_without_schema_sends_no_output_config(monkeypatch):
    stub = StubClient(_response("plain text"))
    monkeypatch.setattr(llm, "client", lambda: stub)
    llm.complete(system="s", user_content="u")
    assert "output_config" not in stub.kwargs


def test_complete_raises_on_refusal_but_keeps_usage(monkeypatch):
    stub = StubClient(_response("", stop_reason="refusal"))
    monkeypatch.setattr(llm, "client", lambda: stub)
    with pytest.raises(llm.LLMError) as excinfo:
        llm.complete(system="s", user_content="u")
    # the refused call was still billed — usage must be available for metering
    assert excinfo.value.usage is not None
    assert excinfo.value.usage.input_tokens == 100


def test_complete_wraps_api_errors(monkeypatch):
    import anthropic
    import httpx

    def raising_create(**kwargs):
        raise anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )

    stub = SimpleNamespace(messages=SimpleNamespace(create=raising_create))
    monkeypatch.setattr(llm, "client", lambda: stub)
    with pytest.raises(llm.LLMError) as excinfo:
        llm.complete(system="s", user_content="u")
    assert excinfo.value.usage is None  # request never completed — nothing billed


def test_client_requires_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(llm.LLMNotConfigured):
        llm.client()


# -- profile -----------------------------------------------------------------
def test_load_profile_missing_returns_empty(data_dir):
    assert profile.load_profile() == ""


def test_load_profile_reads_and_strips(data_dir):
    from jobdeck import config

    config.PROFILE_PATH.write_text("  Python developer  \n", encoding="utf-8")
    assert profile.load_profile() == "Python developer"


# -- scoring -----------------------------------------------------------------
def test_score_job_parses_clamps_and_strips(monkeypatch):
    def fake_complete(**kwargs):
        return llm.LLMResult(
            text='{"score": 140, "reason": " Sehr guter Fit. "}',
            model="m", input_tokens=1, output_tokens=1, cost_usd=0.0,
        )

    monkeypatch.setattr(llm, "complete", fake_complete)
    score, reason, usage = scoring.score_job(_job(), "profile text")
    assert score == 100
    assert reason == "Sehr guter Fit."
    assert usage.input_tokens == 1


def test_score_job_rejects_unparseable_response(monkeypatch):
    def fake_complete(**kwargs):
        return llm.LLMResult(
            text="not json", model="m", input_tokens=1, output_tokens=1, cost_usd=0.0
        )

    monkeypatch.setattr(llm, "complete", fake_complete)
    with pytest.raises(llm.LLMError) as excinfo:
        scoring.score_job(_job(), "profile text")
    # the malformed-but-billed call must expose its usage for metering
    assert excinfo.value.usage is not None


def test_build_user_content_includes_job_and_truncates(monkeypatch):
    job = _job(description="x" * (scoring.MAX_DESCRIPTION_CHARS + 500), remote=1)
    content = scoring.build_user_content(job, "my profile")
    assert "my profile" in content
    assert "Firma GmbH" in content
    assert "(remote)" in content
    # truncated to exactly the cap — neither dropped nor passed through whole
    assert content.count("x") == scoring.MAX_DESCRIPTION_CHARS
