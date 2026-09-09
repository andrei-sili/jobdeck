"""How a posting's five sub-scores become the one number the list is ordered on.

The model rates a posting on five dimensions; the weights are the candidate's
and live in app_settings. Combining them in code, not in the prompt, is what
lets a weight change re-order the list from stored numbers without a model
call — and what makes the order explainable: "Stack 70, Niveau 40" says why a
posting sits where it does, a bare 61 does not.

Pure, deliberately: no SQL, no settings access. `db` reads the weights and
writes the columns, `ai.scoring` reads the model's numbers, the Stellen pane
prints the labels, and all three share these definitions so a dimension added
here reaches the prompt, the schema, the table and the screen together.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Dimension:
    """One axis the model rates a posting on."""

    key: str      # the model's field name and the settings-key suffix
    column: str   # the jobs column the number is stored in
    label: str    # German, as the Stellen pane and Einstellungen print it
    default: int  # weight when nothing is stored
    asks: str     # what the model is told to rate, in the prompt's words


DIMENSIONS: tuple[Dimension, ...] = (
    Dimension(
        "role", "score_role", "Rolle", 30,
        "the KIND of work: application development (backend, full-stack, "
        "DevOps-adjacent) as the profile describes it fits; data science, "
        "embedded, administration, consulting or sales do not, however "
        "familiar the tools",
    ),
    Dimension(
        "stack", "score_stack", "Stack", 30,
        "the languages, frameworks and tools the posting names, against those "
        "the profile holds",
    ),
    Dimension(
        "level", "score_level", "Niveau", 20,
        "the experience level the posting demands against what the profile "
        "offers: an entry-level or junior position fits, mandatory years of "
        "experience or a lead role do not",
    ),
    Dimension(
        "language", "score_language", "Sprache", 10,
        "the working language and any language requirement, against the "
        "profile's languages",
    ),
    Dimension(
        "conditions", "score_conditions", "Rahmen", 10,
        "the terms: location and remote model, stated pay against the "
        "profile's expectation, contract type and hours",
    ),
)
KEYS: tuple[str, ...] = tuple(d.key for d in DIMENSIONS)
COLUMNS: tuple[str, ...] = tuple(d.column for d in DIMENSIONS)

SETTING_PREFIX = "score_weight_"
# What the model answers when the posting states nothing about a dimension.
# Stored as NULL, never as a number: missing information is neutral, and a
# neutral axis is left out of the mean rather than dragging it down.
UNKNOWN = -1
MAX_WEIGHT = 100
MAX_SCORE = 100


def setting_key(key: str) -> str:
    """The app_settings key holding the weight of one dimension."""
    return f"{SETTING_PREFIX}{key}"


def default_weights() -> dict[str, int]:
    return {d.key: d.default for d in DIMENSIONS}


def _whole(raw: object) -> int | None:
    """A stored or reported number as an int; None for anything else.

    bool is refused on purpose — it is an int to Python and a mistake here."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    try:
        text = str(raw).strip()
        return int(text) if text else None
    except (TypeError, ValueError):
        return None


def parse_weights(raw: Mapping[str, object]) -> dict[str, int]:
    """The candidate's weights from stored strings.

    Each is clamped to 0..MAX_WEIGHT and falls back to its default when
    unreadable — a hand-edited setting must never take the list down. All
    five at zero would order nothing, so that reads as "no preference": the
    defaults."""
    weights = {}
    for dim in DIMENSIONS:
        value = _whole(raw.get(dim.key))
        weights[dim.key] = (dim.default if value is None
                            else max(0, min(MAX_WEIGHT, value)))
    if not any(weights.values()):
        return default_weights()
    return weights


def clamp_subscores(raw: Mapping[str, object]) -> dict[str, int | None]:
    """The model's five numbers as stored: 0..MAX_SCORE, or None for UNKNOWN.

    The caller has already established that every key is present and an
    integer (the schema promises it, the parser checks it); this only bounds
    the values, the way the overall score is bounded."""
    clamped = {}
    for dim in DIMENSIONS:
        value = _whole(raw.get(dim.key))
        clamped[dim.key] = (None if value is None or value < 0
                            else min(MAX_SCORE, value))
    return clamped


def weighted(subscores: Mapping[str, int | None],
             weights: Mapping[str, int]) -> int | None:
    """The weighted mean over the dimensions the posting states, rounded.

    None when no dimension is known, or when every known one carries weight
    zero — then the stored numbers say nothing about the order and the caller
    keeps the model's overall judgement. Floored at 1: 0 is the knock-out
    sentinel and only a violated hard requirement may produce it."""
    total = 0.0
    weight_sum = 0
    for dim in DIMENSIONS:
        value = subscores.get(dim.key)
        weight = weights.get(dim.key, 0)
        if value is None or weight <= 0:
            continue
        total += weight * value
        weight_sum += weight
    if weight_sum == 0:
        return None
    return max(1, int(total / weight_sum + 0.5))
