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


def test_delete_bewerbung_clears_references(con):
    job_id = _add_job(con)
    bewerbung_id = db.apply_job(con, job_id, kanal="E-Mail")
    db.delete_bewerbung(con, bewerbung_id)
    assert db.get_bewerbung(con, bewerbung_id) is None
    assert db.get_job(con, job_id)["bewerbung_id"] is None
