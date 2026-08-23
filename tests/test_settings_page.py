"""Data-layer behaviour of the settings page (no NiceGUI rendering)."""

from jobdeck import db
from jobdeck import settings as app_settings
from jobdeck.services import autosend, mappe, preparing, send
from jobdeck.ui.pages import dashboard, settings


def test_mappe_budgets_default_when_unset(con, data_dir):
    loaded = settings._get_settings()
    assert loaded["mappe_compress"] is True  # on unless switched off
    assert loaded["mappe_target_mb"] == mappe.DEFAULT_TARGET_MB
    assert loaded["mappe_target_portal_mb"] == mappe.DEFAULT_PORTAL_TARGET_MB


def test_a_hand_edited_budget_does_not_break_the_page(con, data_dir):
    """app_settings holds strings and the data dir is the user's to edit; a
    bad value must show the budget that will really be used, not raise on
    float() and take the whole Settings page down with it."""
    db.set_setting(con, "mappe_target_mb", "not a number")
    db.set_setting(con, "mappe_target_portal_mb", "-3")
    con.commit()

    loaded = settings._get_settings()
    assert loaded["mappe_target_mb"] == mappe.DEFAULT_TARGET_MB
    assert loaded["mappe_target_portal_mb"] == mappe.DEFAULT_PORTAL_TARGET_MB


def test_saved_budgets_round_trip(con, data_dir):
    db.set_setting(con, "mappe_target_mb", "2.5")
    db.set_setting(con, "mappe_compress", "0")
    con.commit()

    loaded = settings._get_settings()
    assert loaded["mappe_target_mb"] == 2.5
    assert loaded["mappe_compress"] is False


def test_typed_parsers_handle_invalid_finite_and_bounded_values():
    assert app_settings.parse_int("not a number", 14, minimum=1) == 14
    assert app_settings.parse_int("1e999", 14, allow_decimal=True) == 14
    assert app_settings.parse_int("-8", 14, minimum=0) == 0
    assert app_settings.parse_int("-8", 14, minimum=1, clamp=False) == 14
    assert app_settings.parse_int("8.9", 14, allow_decimal=True) == 8
    assert app_settings.parse_int("999", 14, maximum=365) == 365
    assert app_settings.parse_bool("1") is True
    assert app_settings.parse_bool("0", True) is False
    assert app_settings.parse_bool("yes") is False
    assert app_settings.parse_float("inf", 2.5, minimum_exclusive=0) == 2.5


def test_corrupt_core_settings_use_the_same_safe_defaults_everywhere(
    con, data_dir
):
    for key, value in {
        "follow_up_days": "invalid",
        "daily_send_cap": "1e999",
        "real_send_enabled": "yes",
        "prepare_max_age_days": "infinite",
        "prepare_min_score": "bad",
        "prepare_per_day": "bad",
        "prepare_include_forms": "maybe",
        "llm_cost_usd": "inf",
        "test_recipient": "test@example.org",
    }.items():
        db.set_setting(con, key, value)
    con.commit()

    loaded = settings._get_settings()
    assert loaded["follow_up_days"] == 14
    assert loaded["daily_send_cap"] == 15
    assert loaded["real_send_enabled"] is False
    assert loaded["llm_cost_usd"] == 0.0
    assert dashboard._load()["threshold"] == 14
    assert send._load_context(-1)[2]["daily_send_cap"] == 15
    assert autosend._global_block(con) == ""
    assert preparing.settings(con) == {
        "max_age_days": preparing.DEFAULT_MAX_AGE_DAYS,
        "min_score": preparing.DEFAULT_MIN_SCORE,
        "per_day": preparing.DEFAULT_PER_DAY,
        "include_forms": preparing.DEFAULT_INCLUDE_FORMS,
    }


def test_zero_daily_send_cap_remains_a_compatible_hard_stop(con, data_dir):
    db.set_setting(con, "daily_send_cap", "0")
    db.set_setting(con, "test_recipient", "test@example.org")
    con.commit()

    assert settings._get_settings()["daily_send_cap"] == 0
    assert send._load_context(-1)[2]["daily_send_cap"] == 0
    assert autosend._global_block(con) == "daily cap reached"
