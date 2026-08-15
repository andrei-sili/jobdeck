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


def test_opening_a_form_changes_the_data_signature(con, data_dir):
    """A form he opened is drawn on two screens — the reading pane's button
    state and the "Läuft" strip — and the status no longer moves at all when it
    happens. Without a term of its own the strip simply never appears until he
    reloads, and every functional test stays green: this is the class that
    twice demolished the posting he was reading (an unsigned `opened_at`)."""
    job_id = _job(con)
    before = db.data_signature(con)
    db.mark_form_opened(con, job_id)
    con.commit()
    assert db.data_signature(con) != before


def test_staging_a_mappe_changes_the_data_signature(con, data_dir):
    """The strip says whether the documents are ready, and the build that makes
    them ready runs in the background — so nothing but this term can tell the
    open page that "Mappe wird gebaut …" has become "Mappe fertig"."""
    job_id = _job(con)
    db.mark_form_opened(con, job_id)
    con.commit()
    before = db.data_signature(con)
    db.set_upload(con, job_id, "/tmp/Bewerbung.pdf", "vollständig")
    con.commit()
    assert db.data_signature(con) != before


def test_taking_back_an_opened_form_changes_the_data_signature(con, data_dir):
    """And the way back out again: the entry has to leave the strip by itself
    on the screen he pressed it from, and on any other tab that is open."""
    job_id = _job(con)
    db.mark_form_opened(con, job_id)
    db.set_upload(con, job_id, "/tmp/Bewerbung.pdf", "vollständig")
    con.commit()
    before = db.data_signature(con)
    db.clear_form_opened(con, job_id)
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


def test_setting_a_posting_aside_changes_the_data_signature(con, data_dir):
    """He can mark a posting in one tab; the other has to notice, or its
    Vorgemerkt count and its list would disagree with the database."""
    job_id = _job(con)
    before = db.data_signature(con)
    db.set_bookmark(con, job_id, True)
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
def _is_signature_call(node: ast.Call) -> bool:
    """Whether this call reads a signature, however it is spelled.

    The rule used to test `ast.unparse(node.func).endswith("_signature")`,
    which matches `db.data_signature(...)` and `_signature(...)` — and misses
    `unterlagen.signature(con, job_id)` by one character. The Unterlagen
    loader was therefore skipped in silence: the rule reported success while
    the newest screen was the one thing it did not check. Match the NAME being
    called instead of the spelling of the path to it.
    """
    name = (node.func.attr if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", ""))
    return name == "signature" or name.endswith("_signature")


def _composes_a_signature(func) -> bool:
    """Whether this function IS a signature rather than a loader that reads one.

    A signature composer has no data to label — every read inside it is part
    of the same one-moment snapshot, so their order carries nothing. Judging
    it by the loader rule turns a correct signature into a failure and invites
    reordering real code to satisfy a rule that does not apply to it.
    """
    return func.name == "signature" or func.name.endswith("_signature")


def _signature_or_data_calls(func) -> list[str]:
    """Every read a loader makes, in source order, tagged by what it is."""
    found = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if _is_signature_call(node):
            found.append((node.lineno, "signature"))
        elif (isinstance(node.func, ast.Attribute)
                and getattr(node.func.value, "id", "") == "db"
                and node.func.attr != "db"):
            found.append((node.lineno, node.func.attr))
    return [kind for _, kind in sorted(found)]


def _loader_files() -> list[pathlib.Path]:
    """Every file that can hold a loader: the whole UI package, and the
    services that read a signature on a page's behalf.

    `services/unterlagen.py` reads one for the Unterlagen screen and lives
    outside `ui/`, so a scan bounded by the UI package never opened it.
    """
    from jobdeck.ui.pages import jobs as jobs_page
    ui_dir = pathlib.Path(jobs_page.__file__).parent.parent
    services_dir = ui_dir.parent / "services"
    return sorted([*ui_dir.rglob("*.py"), *services_dir.rglob("*.py")])


def test_every_loader_reads_its_signature_before_the_data_it_describes():
    """sqlite3 gives each SELECT its own read snapshot, so a poll committing
    between the rows and the signature would marry STALE rows to a FRESH
    signature — which the watcher then records as "what the page is showing",
    and the change is never rendered. Reading the signature first can only fail
    the safe way: one rebuild nobody needed.

    Over the whole UI package, not only its pages: the rail is a loader that
    lives beside them and is rendered on every screen there is."""
    checked = []
    for path in _loader_files():
        tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if _composes_a_signature(func):
                continue
            calls = _signature_or_data_calls(func)
            if "signature" not in calls or len(calls) == 1:
                continue
            assert calls[0] == "signature", (
                f"{path.name}:{func.name} reads {calls[0]} before its "
                f"signature — a write landing between the two would be lost")
            checked.append(f"{path.name}:{func.name}")
    assert len(checked) >= 5, f"the scan found almost nothing: {checked}"


def test_the_signature_first_rule_actually_covers_the_newest_screen():
    """The rule passed for weeks on a page it was silently skipping, because
    its detector matched the SPELLING of the call rather than the name. A rule
    that can quietly cover nothing has to say what it covered."""
    covered = []
    for path in _loader_files():
        tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if _composes_a_signature(func):
                continue
            calls = _signature_or_data_calls(func)
            if "signature" in calls and len(calls) > 1:
                covered.append(f"{path.stem}:{func.name}")

    assert "unterlagen:_load" in covered, (
        f"the Unterlagen loader is not checked by the signature-first rule; "
        f"covered: {sorted(covered)}")


def test_the_rule_recognises_a_signature_read_however_it_is_spelled():
    """The detector matched the SPELLING of the call path, so
    `unterlagen.signature(con, job_id)` — one character away from
    `*_signature` — was not recognised and its loader was skipped in silence
    for the whole slice. Pin every spelling the codebase actually uses."""
    for source in ("db.data_signature(con)", "_signature(con)",
                   "unterlagen.signature(con, job_id)",
                   "unterlagen_service.signature(con, job_id)"):
        call = ast.parse(source, mode="eval").body
        assert _is_signature_call(call), f"not recognised: {source}"

    for source in ("db.list_claims(con)", "read(con, job_id)",
                   "db.get_setting(con, 'x', '')"):
        call = ast.parse(source, mode="eval").body
        assert not _is_signature_call(call), f"wrongly recognised: {source}"


def test_reading_a_posting_changes_the_data_signature(con, data_dir):
    """"Neu" is a count of what he has not opened, so another tab has to see
    him open one — otherwise its rail keeps promising work already done."""
    job_id = _job(con)
    before = db.data_signature(con)
    db.mark_job_opened(con, job_id)
    con.commit()
    assert db.data_signature(con) != before
