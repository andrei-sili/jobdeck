"""Filling the permission register from the profile, on demand and confirmed.

One model call, on the user's press. It PROPOSES; nothing reaches the register
until he confirms, because the register is the list of things a letter may say
about him and a model's reading of his own file is not authority for that.

Entries the register already holds are filtered out here rather than merged in
the UI: the register is a set of permissions, and a second row saying the same
thing would split its counter in two.
"""

import asyncio
import logging

from jobdeck import claims as claims_lib
from jobdeck import db
from jobdeck.ai import claims as ai_claims
from jobdeck.ai import llm, profile

log = logging.getLogger(__name__)

# Every other spending service in this app is single-flight, and this one is
# reached by a button whose only feedback is a toast that fades while the call
# runs — the shape that gets pressed three times and billed three times.
_lock = asyncio.Lock()


def _key(fact: str, binding: str) -> tuple[str, str]:
    """What makes two entries the same permission: the pair, folded.

    The pair rather than the fact alone — the same competence bound to two
    different projects is two permissions, and collapsing them is exactly the
    weld the register exists to prevent.
    """
    from jobdeck.dedupe import fold
    return (fold(fact).strip(), fold(binding).strip())


def _existing_keys() -> set[tuple[str, str]]:
    with db.db() as con:
        return {_key(row["fact"], row["binding"]) for row in db.list_claims(con)}


def _record_usage(usage: llm.LLMResult) -> None:
    with db.db() as con:
        db.record_llm_usage(con, usage.input_tokens, usage.output_tokens,
                            usage.cost_usd)


def _enabled() -> bool:
    """The master AI switch's promise is that nothing is sent while it is off."""
    with db.db() as con:
        return db.ai_enabled(con)


def _propose() -> dict:
    if not _enabled():
        return {"ok": False, "error": "KI ist in den Einstellungen "
                                      "ausgeschaltet", "claims": []}
    profile_text = profile.load_profile()
    if not profile_text:
        return {"ok": False, "error": "profile.md ist leer oder fehlt — daraus "
                                      "gibt es nichts zu lesen", "claims": []}
    try:
        proposed, usage = ai_claims.extract_claims(profile_text)
    except llm.LLMNotConfigured:
        # NOT a subclass of LLMError — a sibling — so catching LLMError alone
        # let it escape to_thread, past the page's await, into NiceGUI's
        # handler wrapper, which turns it into a log line. The button then did
        # nothing and said nothing, however often it was pressed. Every other
        # spending service names this case; this one has to as well.
        return {"ok": False, "claims": [],
                "error": "Kein Anthropic-Schlüssel geladen — in "
                         "secrets.env eintragen"}
    except llm.LLMError as exc:
        # The call may have billed before failing; meter it either way.
        if exc.usage is not None:
            _record_usage(exc.usage)
        log.info("claims: extraction failed: %s", exc)
        return {"ok": False, "error": str(exc), "claims": []}
    _record_usage(usage)

    known = _existing_keys()
    fresh, skipped = [], 0
    for claim in proposed:
        if _key(claim["fact"], claim["binding"]) in known:
            skipped += 1
            continue
        known.add(_key(claim["fact"], claim["binding"]))
        fresh.append({**claim,
                      "headline": claims_lib.headline(claim)})
    return {"ok": True, "error": "", "claims": fresh, "skipped": skipped,
            "cost_usd": usage.cost_usd}


async def propose_from_profile() -> dict:
    """Read profile.md and propose register entries the register lacks.

    Returns {"ok", "error", "claims", "skipped", "cost_usd"}. Writes nothing
    but the spend meter. A second press while one reading is in flight is
    refused rather than queued: the answer would be the same proposal, and
    paying twice for it is the whole failure mode.
    """
    if _lock.locked():
        return {"ok": False, "claims": [], "skipped": 0, "cost_usd": 0.0,
                "error": "profile.md wird bereits gelesen"}
    async with _lock:
        return await asyncio.to_thread(_propose)


def _store(chosen: list[dict]) -> int:
    with db.db() as con:
        known = {_key(row["fact"], row["binding"])
                 for row in db.list_claims(con)}
        written = 0
        for claim in chosen:
            # Re-checked against the register as it is NOW: the proposal was
            # made before he read it, and he may have typed one of these in by
            # hand in the meantime.
            key = _key(claim["fact"], claim.get("binding", ""))
            if key in known:
                continue
            known.add(key)
            db.add_claim(con, claim)
            written += 1
    return written


async def accept(chosen: list[dict]) -> int:
    """Write the proposals he kept. Returns how many were actually added."""
    return await asyncio.to_thread(_store, chosen)
