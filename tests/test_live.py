"""The self-refresh mechanism: when a tick asks, and what it does with the
answer. The decision is pure so it can be pinned without a browser; the wiring
is exercised by the page-rendering tests."""

import ast
import pathlib

from jobdeck import db
from jobdeck.ui import live


# --------------------------------------------------------------------------
# should_check — the fast/slow beat
# --------------------------------------------------------------------------
def test_a_page_without_a_hot_state_asks_on_every_tick():
    assert all(live.should_check(n, hot=False, idle_every=1) for n in range(1, 8))


def test_a_hot_page_asks_on_every_tick_however_slow_the_idle_beat_is():
    assert all(live.should_check(n, hot=True, idle_every=6) for n in range(1, 13))


def test_an_idle_page_asks_only_every_nth_tick():
    asked = [n for n in range(1, 13)
             if live.should_check(n, hot=False, idle_every=6)]
    assert asked == [6, 12]


# --------------------------------------------------------------------------
# tick_action — rebuild, defer, or nothing
# --------------------------------------------------------------------------
def test_nothing_changed_means_nothing_happens():
    assert live.tick_action(changed=False, busy=False) == live.IDLE
    assert live.tick_action(changed=False, busy=True) == live.IDLE


def test_a_change_on_an_idle_page_rebuilds_it():
    assert live.tick_action(changed=True, busy=False) == live.REBUILD


def test_a_change_while_he_is_reading_waits_for_him():
    """A rebuild collapses every open expansion and deletes an open dialog —
    so while either is on screen the fresh data waits behind the chip."""
    assert live.tick_action(changed=True, busy=True) == live.DEFER


# --------------------------------------------------------------------------
# The signatures the ticks compare
# --------------------------------------------------------------------------
def _job(con, **over):
    values = {"source": "stub", "external_id": "j1", "title": "Dev",
              "company": "Firma GmbH"}
    values.update(over)
    job_id = db.insert_job_if_new(con, values)
    con.commit()
    return job_id


def test_a_new_posting_changes_the_data_signature(con, data_dir):
    before = db.data_signature(con)
    _job(con)
    assert db.data_signature(con) != before


def test_a_score_changes_the_data_signature(con, data_dir):
    job_id = _job(con)
    before = db.data_signature(con)
    db.set_job_score(con, job_id, 87, "fits")
    con.commit()
    assert db.data_signature(con) != before


def test_a_liveness_verdict_changes_the_data_signature(con, data_dir):
    job_id = _job(con)
    before = db.data_signature(con)
    db.set_job_liveness(con, job_id, "gone")
    con.commit()
    assert db.data_signature(con) != before


def test_a_status_change_changes_the_data_signature(con, data_dir):
    """The row count does not move when a posting is skipped — the per-status
    counts are what makes it visible."""
    job_id = _job(con)
    before = db.data_signature(con)
    db.set_job_status(con, job_id, "skipped")
    con.commit()
    assert db.data_signature(con) != before


def test_an_adopted_contact_email_changes_the_data_signature(con, data_dir):
    job_id = _job(con)
    before = db.data_signature(con)
    db.set_contact_email(con, job_id, "hr@firma.de", "web_lookup")
    con.commit()
    assert db.data_signature(con) != before


def test_a_draft_moving_changes_the_data_signature(con, data_dir):
    job_id = _job(con)
    db.upsert_draft(con, job_id, {"status": "generating"})
    con.commit()
    before = db.data_signature(con)
    db.upsert_draft(con, job_id, {"status": "ready", "betreff": "B"})
    con.commit()
    assert db.data_signature(con) != before


def test_a_recorded_application_changes_the_data_signature(con, data_dir):
    before = db.data_signature(con)
    db.add_bewerbung(con, {"firma": "Firma GmbH", "status": "Gesendet"})
    con.commit()
    assert db.data_signature(con) != before


def test_an_application_status_change_changes_the_data_signature(con, data_dir):
    """The job inbox quotes how an application ENDED, so a posting's warning
    line goes stale when a status moves."""
    row_id = db.add_bewerbung(con, {"firma": "Firma GmbH", "status": "Gesendet"})
    con.commit()
    before = db.data_signature(con)
    db.set_status(con, row_id, "Absage", source="manual")
    con.commit()
    assert db.data_signature(con) != before


def test_reading_twice_without_a_write_gives_the_same_signature(con, data_dir):
    _job(con)
    assert db.data_signature(con) == db.data_signature(con)


def test_llm_spend_changes_the_meter_signature(con, data_dir):
    before = db.meter_signature(con)
    db.record_llm_usage(con, input_tokens=10, output_tokens=5, cost_usd=0.01)
    con.commit()
    assert db.meter_signature(con) != before


def test_the_meter_signature_ignores_a_setting_he_is_editing(con, data_dir):
    """Settings polls the meters only: the page must never overwrite an input
    while he is typing in it."""
    before = db.meter_signature(con)
    db.set_setting(con, "test_recipient", "someone@example.org")
    con.commit()
    assert db.meter_signature(con) == before


def test_a_poll_changes_the_profiles_signature(con, data_dir):
    profile_id = db.add_profile(con, {"name": "P", "keywords": "python"})
    con.commit()
    before = db.profiles_signature(con)
    db.mark_profile_polled(con, profile_id, error="")
    con.commit()
    assert db.profiles_signature(con) != before


def test_a_poll_error_changes_the_profiles_signature(con, data_dir):
    profile_id = db.add_profile(con, {"name": "P", "keywords": "python"})
    db.mark_profile_polled(con, profile_id, error="")
    con.commit()
    before = db.profiles_signature(con)
    db.mark_profile_polled(con, profile_id, error="jooble: 401")
    con.commit()
    assert db.profiles_signature(con) != before


# --------------------------------------------------------------------------
# The order inside a loader is load-bearing
# --------------------------------------------------------------------------
def _db_calls(func) -> list[str]:
    """`db.<name>(...)` calls inside a function, in source order, minus the
    connection itself."""
    names = []
    for node in ast.walk(func):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and getattr(node.func.value, "id", "") == "db"
                and node.func.attr != "db"):
            names.append((node.lineno, node.func.attr))
    return [name for _, name in sorted(names)]


def test_every_loader_reads_its_signature_before_the_data_it_describes():
    """sqlite3 gives each SELECT its own read snapshot, so a poll committing
    between the rows and the signature would marry STALE rows to a FRESH
    signature — which the watcher then records as "what the page is showing",
    and the change is never rendered. Reading the signature first can only fail
    the safe way: one rebuild nobody needed."""
    from jobdeck.ui.pages import jobs as jobs_page

    pages = pathlib.Path(jobs_page.__file__).parent
    checked = []
    for path in sorted(pages.glob("*.py")):
        tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls = _db_calls(func)
            signatures = [c for c in calls if c.endswith("_signature")]
            if not signatures or len(calls) == 1:
                continue
            assert calls[0].endswith("_signature"), (
                f"{path.name}:{func.name} reads {calls[0]} before its "
                f"signature — a write landing between the two would be lost")
            checked.append(f"{path.name}:{func.name}")
    assert len(checked) >= 4, f"the scan found almost nothing: {checked}"
