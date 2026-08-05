"""Data-layer behaviour of the settings page (no NiceGUI rendering)."""

from jobdeck import db
from jobdeck.services import mappe
from jobdeck.ui.pages import settings


def test_mappe_budgets_default_when_unset(con, data_dir):
    loaded = settings._get_settings()
    assert loaded["mappe_compress"] == "1"  # on unless switched off
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
    assert loaded["mappe_compress"] == "0"
