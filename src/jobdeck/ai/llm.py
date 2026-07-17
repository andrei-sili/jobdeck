"""Thin Anthropic client wrapper: one call in, text plus token/cost accounting out.

Synchronous by design — callers on the event loop go through asyncio.to_thread,
matching the sqlite convention in this codebase. The model is configurable via
ANTHROPIC_MODEL (default claude-haiku-4-5); no sampling parameters are sent so
the same call works on every current model.
"""

import logging
from dataclasses import dataclass

import anthropic

from jobdeck import config

log = logging.getLogger(__name__)

# USD per million tokens (input, output). Matched by prefix so dated snapshot
# ids resolve too; unknown models still meter tokens but report zero cost
# rather than a wrong number.
PRICING_PER_MTOK = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),
}

# Bounds a hung request: SDK default is 10 min x (2 retries + 1) ≈ 30 min,
# which would stall a whole scoring batch. Our calls are small and fast.
REQUEST_TIMEOUT_S = 60.0

_client: anthropic.Anthropic | None = None


class LLMNotConfigured(RuntimeError):
    """No ANTHROPIC_API_KEY available — LLM features are disabled."""


class LLMError(RuntimeError):
    """The API call failed or returned an unusable response.

    `usage` carries the token/cost accounting when the API call itself
    succeeded (e.g. refusal, unparseable output) — those tokens were still
    paid for and must be metered by the caller.
    """

    def __init__(self, message: str, usage: "LLMResult | None" = None):
        super().__init__(message)
        self.usage = usage


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


def pricing(model: str) -> tuple[float, float]:
    """(input, output) USD per MTok for a model id; (0, 0) when unknown."""
    for known, rates in PRICING_PER_MTOK.items():
        if model.startswith(known):
            return rates
    return (0.0, 0.0)


def client() -> anthropic.Anthropic:
    global _client
    if not config.anthropic_api_key():
        raise LLMNotConfigured("ANTHROPIC_API_KEY is not set")
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=config.anthropic_api_key(), timeout=REQUEST_TIMEOUT_S
        )
    return _client


def complete(
    system: str,
    user_content: str,
    max_tokens: int = 1024,
    output_schema: dict | None = None,
    model: str | None = None,
    timeout: float | None = None,
) -> LLMResult:
    """One model call. With output_schema, structured outputs guarantee the
    response text is JSON matching the schema. `model` overrides the default
    (anthropic_model()) so a caller can pick a stronger model per call —
    e.g. drafting on Sonnet while scoring stays on Haiku. `timeout` overrides
    the default 60s bound for a call that legitimately runs longer (a Sonnet
    draft with adaptive thinking) without loosening it for the fast scoring
    batch."""
    kwargs = {}
    if output_schema is not None:
        kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": output_schema}
        }
    api = client()
    if timeout is not None:
        api = api.with_options(timeout=timeout)
    try:
        response = api.messages.create(
            model=model or config.anthropic_model(),
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_content}],
            **kwargs,
        )
    except anthropic.APIError as exc:
        raise LLMError(str(exc)) from exc
    text = next((b.text for b in response.content if b.type == "text"), "")
    in_rate, out_rate = pricing(response.model)
    usage = response.usage
    cost = (usage.input_tokens * in_rate + usage.output_tokens * out_rate) / 1_000_000
    result = LLMResult(
        text=text,
        model=response.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
    )
    if response.stop_reason == "refusal":
        raise LLMError("model declined the request (stop_reason=refusal)", usage=result)
    if response.stop_reason == "max_tokens":
        log.warning("LLM response truncated at %d output tokens", max_tokens)
    return result
