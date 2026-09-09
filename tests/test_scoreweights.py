"""The pure rule that turns five sub-scores into the number the list orders on."""

import pytest

from jobdeck import scoreweights


def test_the_five_dimensions_reach_prompt_table_and_screen_from_one_place():
    assert scoreweights.KEYS == ("role", "stack", "level", "language", "conditions")
    assert scoreweights.COLUMNS == (
        "score_role", "score_stack", "score_level", "score_language",
        "score_conditions")
    assert [d.label for d in scoreweights.DIMENSIONS] == [
        "Rolle", "Stack", "Niveau", "Sprache", "Rahmen"]
    assert scoreweights.setting_key("stack") == "score_weight_stack"
    assert scoreweights.default_weights() == {
        "role": 30, "stack": 30, "level": 20, "language": 10, "conditions": 10}


def test_weights_are_read_from_stored_strings_and_bounded():
    parsed = scoreweights.parse_weights({
        "role": "50", "stack": " 0 ", "level": "140", "language": "-3",
        "conditions": "zehn",
    })
    assert parsed == {"role": 50, "stack": 0, "level": 100, "language": 0,
                      "conditions": 10}  # unreadable → its default


@pytest.mark.parametrize("raw", [
    {},                                   # nothing stored yet
    {"role": "", "stack": ""},            # blank fields
    {k: "0" for k in scoreweights.KEYS},  # all zero orders nothing
    {"role": True},                       # a bool is a mistake, not a weight
])
def test_no_preference_reads_as_the_defaults(raw):
    assert scoreweights.parse_weights(raw) == scoreweights.default_weights()


def test_the_models_numbers_are_bounded_and_unknown_becomes_none():
    clamped = scoreweights.clamp_subscores({
        "role": 80, "stack": 130, "level": -1, "language": -7,
        "conditions": "55",
    })
    assert clamped == {"role": 80, "stack": 100, "level": None,
                       "language": None, "conditions": 55}
    assert scoreweights.clamp_subscores({}) == dict.fromkeys(scoreweights.KEYS)


def test_the_weighted_mean_leaves_out_what_the_posting_did_not_state():
    weights = scoreweights.default_weights()
    all_known = {"role": 80, "stack": 60, "level": 100, "language": 100,
                 "conditions": 50}
    # (30*80 + 30*60 + 20*100 + 10*100 + 10*50) / 100 = 77
    assert scoreweights.weighted(all_known, weights) == 77
    # conditions unknown: renormalised over the other 90 points of weight
    partial = {**all_known, "conditions": None}
    # (2400 + 1800 + 2000 + 1000) / 90 = 80
    assert scoreweights.weighted(partial, weights) == 80
    # a weight moves the answer without any new number
    stack_heavy = {**weights, "stack": 100, "role": 0}
    # (100*60 + 20*100 + 10*100 + 10*50) / 140 = 67.86 → 68
    assert scoreweights.weighted(all_known, stack_heavy) == 68


def test_nothing_known_or_nothing_weighted_yields_no_number():
    weights = scoreweights.default_weights()
    assert scoreweights.weighted(dict.fromkeys(scoreweights.KEYS), weights) is None
    only_conditions = {**dict.fromkeys(scoreweights.KEYS), "conditions": 90}
    assert scoreweights.weighted(only_conditions,
                                 {**weights, "conditions": 0}) is None
    assert scoreweights.weighted({"role": 90}, {}) is None


def test_the_combination_never_produces_the_knock_out_sentinel():
    """0 belongs to a violated hard requirement alone."""
    zeros = dict.fromkeys(scoreweights.KEYS, 0)
    assert scoreweights.weighted(zeros, scoreweights.default_weights()) == 1
    assert scoreweights.weighted({"role": 0}, {"role": 100}) == 1


def test_rounding_is_to_the_nearest_whole_number():
    weights = {"role": 1, "stack": 1}
    assert scoreweights.weighted({"role": 50, "stack": 51}, weights) == 51  # 50.5 up
    assert scoreweights.weighted({"role": 50, "stack": 50}, weights) == 50
    assert scoreweights.weighted({"role": 33, "stack": 34}, weights) == 34  # 33.5 up


def test_a_whole_float_is_a_number_and_a_fraction_is_not():
    """A number input hands over 30.0 for 30."""
    assert scoreweights.parse_weights({"stack": 70.0})["stack"] == 70
    assert scoreweights.parse_weights({"stack": 2.5})["stack"] == 30  # default
    assert scoreweights.clamp_subscores({"role": 80.0})["role"] == 80
