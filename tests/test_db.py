from jobdeck import db


def _add_app(con, firma="Testfirma GmbH", email="jobs@testfirma.de", status="Gesendet"):
    return db.add_bewerbung(
        con,
        {"gesendet_am": "2026-07-01", "firma": firma, "email": email,
         "kanal": "E-Mail", "status": status},
    )


def _add_job(con, **over):
    values = {
        "source": "arbeitsagentur",
        "external_id": over.pop("external_id", "REF-1"),
        "title": "Python Entwickler (m/w/d)",
        "company": "Neue Firma GmbH",
        "location": "Aachen",
        "url": "https://example.org/job/1",
        "contact_email": "hr@neuefirma.de",
    }
    values.update(over)
    return db.insert_job_if_new(con, values)


def test_set_status_writes_history(con):
    app_id = _add_app(con)
    assert db.set_status(con, app_id, "Absage", source="user")
    history = db.list_status_history(con, app_id)
    # creation entry + change entry
    assert [h["new_status"] for h in history] == ["Absage", "Gesendet"]
    assert db.get_bewerbung(con, app_id)["status"] == "Absage"


def test_automatic_source_cannot_downgrade(con):
    app_id = _add_app(con, status="Einladung")
    # a late confirmation e-mail must not overwrite the invitation
    assert not db.set_status(con, app_id, "In Bearbeitung", source="reply_rule")
    assert db.get_bewerbung(con, app_id)["status"] == "Einladung"


def test_manual_change_can_downgrade(con):
    app_id = _add_app(con, status="Einladung")
    assert db.set_status(con, app_id, "In Bearbeitung", source="user")
    assert db.get_bewerbung(con, app_id)["status"] == "In Bearbeitung"


def test_insert_job_if_new_is_idempotent(con):
    assert _add_job(con) is not None
    assert _add_job(con) is None  # same (source, external_id)
    assert _add_job(con, external_id="REF-2") is not None


def test_apply_job_creates_application_and_links(con):
    job_id = _add_job(con)
    bewerbung_id = db.apply_job(con, job_id, kanal="Online-Portal")
    assert bewerbung_id is not None
    job = db.get_job(con, job_id)
    assert job["status"] == "applied" and job["bewerbung_id"] == bewerbung_id
    app = db.get_bewerbung(con, bewerbung_id)
    assert app["firma"] == "Neue Firma GmbH"
    assert app["email"] == "hr@neuefirma.de"
    assert app["kanal"] == "Online-Portal"


def test_apply_job_blocks_duplicates(con):
    _add_app(con, firma="Neue Firma GmbH", email="")
    job_id = _add_job(con)
    assert db.apply_job(con, job_id, kanal="E-Mail") is None
    job = db.get_job(con, job_id)
    assert job["status"] == "duplicate" and job["duplicate_of"] is not None


def test_settings_roundtrip(con):
    assert db.get_setting(con, "missing", "fallback") == "fallback"
    db.set_setting(con, "daily_send_cap", "15")
    db.set_setting(con, "daily_send_cap", "20")
    assert db.get_setting(con, "daily_send_cap") == "20"


def test_profiles_crud(con):
    pid = db.add_profile(
        con, {"name": "Python bundesweit", "keywords": "Python Entwickler"}
    )
    profiles = db.list_profiles(con)
    assert len(profiles) == 1 and profiles[0]["auto_send"] == 0
    db.update_profile(
        con, pid,
        {"name": "Python DE", "keywords": "Python", "active": 0},
    )
    assert db.list_profiles(con, active_only=True) == []
    db.delete_profile(con, pid)
    assert db.list_profiles(con) == []


def test_profile_match_criteria_roundtrip(con):
    pid = db.add_profile(
        con,
        {"name": "Backend DE", "keywords": "Python Backend",
         "hard_tags": "#backend\n#münchen", "soft_preferences": "Gehalt 45000 @80%",
         "strictness": 70},
    )
    row = db.get_profile(con, pid)
    assert row["hard_tags"] == "#backend\n#münchen"
    assert row["soft_preferences"] == "Gehalt 45000 @80%"
    assert row["strictness"] == 70

    db.update_profile(
        con, pid,
        {"name": "Backend DE", "keywords": "Python Backend", "hard_tags": "#remote"},
    )
    row = db.get_profile(con, pid)
    assert row["hard_tags"] == "#remote"
    assert row["strictness"] == 50  # unset fields fall back to defaults

    assert db.get_profile(con, 99999) is None


def test_list_jobs_hides_and_counts_mismatches(con):
    ok = _add_job(con, external_id="ok")
    mismatch = _add_job(con, external_id="mismatch")
    unscored = _add_job(con, external_id="unscored")
    db.set_job_score(con, ok, 70, "Passt.")
    db.set_job_score(con, mismatch, 0, "Verstößt gegen #backend.")

    visible = db.list_jobs(con, status="new", mismatches="exclude")
    assert [r["id"] for r in visible] == [ok, unscored]  # NULL score stays visible

    everything = db.list_jobs(con, status="new")
    assert {r["id"] for r in everything} == {ok, mismatch, unscored}

    # the hidden pile stays reachable even when better rows fill the limit
    assert [r["id"] for r in db.list_jobs(con, status="new", limit=1,
                                          mismatches="only")] == [mismatch]

    assert db.count_mismatches(con, status="new") == 1
    assert db.count_mismatches(con, status="applied") == 0
    assert db.count_mismatches(con) == 1

    # the all-statuses view filters too (id DESC, exact rows)
    assert [r["id"] for r in db.list_jobs(con, mismatches="exclude")] \
        == [unscored, ok]


def test_list_jobs_sorts_by_score_then_newest(con):
    """'Score sorts' is the core product rule — pin direction and tiebreak."""
    low = _add_job(con, external_id="low")
    high_old = _add_job(con, external_id="high-old")
    high_new = _add_job(con, external_id="high-new")
    unscored = _add_job(con, external_id="unscored")
    db.set_job_score(con, low, 30, "Teilweise.")
    db.set_job_score(con, high_old, 90, "Sehr gut.")
    db.set_job_score(con, high_new, 90, "Sehr gut.")

    rows = db.list_jobs(con, status="new")
    # best score first, newer id wins the tie, unscored (NULL) last
    assert [r["id"] for r in rows] == [high_new, high_old, low, unscored]


def test_set_job_contacts_fills_only_empty_columns(con):
    job_id = _add_job(con)  # source already provides contact_email
    db.set_job_contacts(con, job_id, {
        "ansprechpartner": " Frau Muster ",
        "contact_email": "extracted@other.de",   # must NOT clobber source data
        "contact_phone": "+49 241 123456",
        "refnr": "REF-2026-42",
        "bogus_column": "ignored",
        "contact_strasse": "   ",                # whitespace-only → skipped
    })
    job = db.get_job(con, job_id)
    assert job["ansprechpartner"] == "Frau Muster"
    assert job["contact_email"] == "hr@neuefirma.de"  # source data wins
    assert job["contact_phone"] == "+49 241 123456"
    assert job["refnr"] == "REF-2026-42"
    assert job["contact_strasse"] == ""
    assert job["contact_source"] == "posting"

    # a second extraction never overwrites what is already there
    db.set_job_contacts(con, job_id, {"ansprechpartner": "Herr Anders"})
    assert db.get_job(con, job_id)["ansprechpartner"] == "Frau Muster"

    # nothing to fill → no contact_source stamp
    empty_job = _add_job(con, external_id="REF-EMPTY")
    db.set_job_contacts(con, empty_job, {"ansprechpartner": ""})
    assert db.get_job(con, empty_job)["contact_source"] == ""

    # an existing source stamp (e.g. future web enrichment) is never rewritten
    preset = _add_job(con, external_id="REF-PRESET")
    con.execute("UPDATE jobs SET contact_source='web' WHERE id=?", (preset,))
    db.set_job_contacts(con, preset, {"ansprechpartner": "Frau Neu"})
    assert db.get_job(con, preset)["contact_source"] == "web"


def test_upsert_draft_keeps_one_row_per_job(con):
    job_id = _add_job(con)
    draft_id = db.upsert_draft(con, job_id, {
        "status": "ready", "recipient": "hr@neuefirma.de",
        "betreff": "Bewerbung als Python Entwickler – Max Muster",
        "email_body": "Sehr geehrte Damen und Herren, ...",
        "anschreiben_body": "Absatz 1\n\nAbsatz 2", "llm_model": "claude-haiku-4-5",
    })
    row = db.get_draft(con, draft_id)
    assert row["status"] == "ready"
    assert row["betreff"].startswith("Bewerbung als")

    # re-draft updates the same row instead of stacking a second one
    again = db.upsert_draft(con, job_id, {"status": "ready", "betreff": "Neu"})
    assert again == draft_id
    assert con.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
    assert db.get_draft_by_job(con, job_id)["betreff"] == "Neu"

    assert db.get_draft_by_job(con, 99999) is None


def test_delete_bewerbung_clears_references(con):
    job_id = _add_job(con)
    bewerbung_id = db.apply_job(con, job_id, kanal="E-Mail")
    db.delete_bewerbung(con, bewerbung_id)
    assert db.get_bewerbung(con, bewerbung_id) is None
    assert db.get_job(con, job_id)["bewerbung_id"] is None


def test_delete_bewerbung_clears_the_link_a_send_wrote(con):
    """record_send is the first writer of drafts.bewerbung_id — without
    clearing it, deleting any sent application hits the FK constraint."""
    job_id = _add_job(con)
    draft_id = db.upsert_draft(con, job_id, {"status": "ready"})
    bewerbung_id = db.apply_job(con, job_id, kanal="E-Mail")
    db.record_send(con, draft_id, "m-1", "t-1", bewerbung_id)
    assert db.get_draft(con, draft_id)["bewerbung_id"] == bewerbung_id

    db.delete_bewerbung(con, bewerbung_id)
    assert db.get_bewerbung(con, bewerbung_id) is None
    assert db.get_draft(con, draft_id)["bewerbung_id"] is None
    assert db.get_draft(con, draft_id)["status"] == "sent"  # history survives
