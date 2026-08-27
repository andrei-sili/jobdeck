"""The UI port comes from the environment, so a second instance can exist.

Hardcoding it meant a verification run against a copy of the data had to take
the port the real app uses. The fallbacks matter as much as the happy path: a
typo in an env var must never be the reason JobDeck will not open.
"""

import pytest

from jobdeck import config


def test_the_port_defaults_when_nothing_is_set(monkeypatch):
    monkeypatch.delenv("JOBDECK_PORT", raising=False)
    assert config.ui_port() == 8123


def test_a_port_in_the_environment_is_used(monkeypatch):
    monkeypatch.setenv("JOBDECK_PORT", "8124")
    assert config.ui_port() == 8124


@pytest.mark.parametrize("raw", [" 8125 ", "\t8125", "8125\n"])
def test_surrounding_whitespace_does_not_defeat_the_setting(monkeypatch, raw):
    """int() tolerates spaces and \n but NOT every form, and the point of the
    strip is the empty-after-strip case below, which "  " covers."""
    monkeypatch.setenv("JOBDECK_PORT", raw)
    assert config.ui_port() == 8125


@pytest.mark.parametrize("raw", ["", "   ", "acht", "80.5", "8123x"])
def test_an_unreadable_value_falls_back_instead_of_raising(monkeypatch, raw):
    # the app must still start: the port is a convenience, not a contract
    monkeypatch.setenv("JOBDECK_PORT", raw)
    assert config.ui_port() == 8123


@pytest.mark.parametrize("raw", ["0", "-1", "65536", "99999", "1", "80", "1023"])
def test_a_port_outside_the_usable_range_falls_back(monkeypatch, raw):
    """bind() would refuse all of these — 1-1023 need root, which this app
    never has — and refusing to start is the worse failure."""
    monkeypatch.setenv("JOBDECK_PORT", raw)
    assert config.ui_port() == 8123


def test_the_lowest_unprivileged_port_is_accepted(monkeypatch):
    monkeypatch.setenv("JOBDECK_PORT", "1024")
    assert config.ui_port() == 1024


def test_run_app_binds_the_configured_port(monkeypatch):
    """The setting is only worth having if the one caller reads it.

    Pinned by driving `run_app`, not by reading the source: a literal restored
    at the call site passes any test that only exercises `ui_port()`.
    """
    from jobdeck.ui import app as ui_app

    seen: dict = {}
    monkeypatch.setattr(ui_app.ui, "run", lambda **kw: seen.update(kw))
    monkeypatch.setattr(ui_app.app, "on_startup", lambda *_: None)
    monkeypatch.setattr(ui_app.app, "on_shutdown", lambda *_: None)
    monkeypatch.setattr(ui_app.config, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(ui_app.config, "load_env", lambda: None)
    monkeypatch.setenv("JOBDECK_PORT", "8199")

    ui_app.run_app()

    assert seen["port"] == 8199
    # the host stays pinned here too: this is an unauthenticated UI with a
    # spend switch on it, and it must never be reachable from the LAN
    assert seen["host"] == "127.0.0.1"
