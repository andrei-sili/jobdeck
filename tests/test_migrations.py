import sqlite3

import pytest

from jobdeck import backup, config, constants, db, migrations


def make_legacy_db(path, rows):
    """Create a database exactly like the legacy tracker did (pre-email/dokument)."""
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE bewerbungen (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            gesendet_am     TEXT,
            firma           TEXT,
            ansprechpartner TEXT,
            strasse         TEXT,
            plz_ort         TEXT,
            kanal           TEXT,
            status          TEXT,
            notiz           TEXT,
            created_at      TEXT
        )
        """
    )
    con.executemany(
        """
        INSERT INTO bewerbungen
            (gesendet_am, firma, ansprechpartner, strasse, plz_ort, kanal, status, notiz,
             created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    con.commit()
    con.close()


LEGACY_ROWS = [
    ("2026-06-10", "Py-T GmbH", "Max Muster", "Weg 1", "52062 Aachen",
     "E-Mail", "Gesendet", "", "2026-06-10T10:00:00"),
    ("2026-06-11", "ACME AG", "", "", "", "Online-Portal", "Absage", "",
     "2026-06-11T10:00:00"),
]


def _tables(con):
    return {
        r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_migrate_legacy_db_preserves_rows_and_adds_tables(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy_db(path, LEGACY_ROWS)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    migrations.migrate(con)

    rows = con.execute("SELECT * FROM bewerbungen ORDER BY id").fetchall()
    assert len(rows) == len(LEGACY_ROWS)
    assert rows[0]["firma"] == "Py-T GmbH"
    # additive columns added by migration
    assert rows[0]["email"] is None and rows[0]["dokument"] is None
    assert {"search_profiles", "jobs", "drafts", "email_log",
            "status_history", "app_settings"} <= _tables(con)
    con.close()


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    make_legacy_db(path, LEGACY_ROWS)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    migrations.migrate(con)
    migrations.migrate(con)  # must not raise or duplicate anything
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 2
    con.close()


def test_migrate_adds_criteria_columns_to_v1_search_profiles(tmp_path):
    """A schema-v1 database (before match criteria) gains the new columns."""
    path = tmp_path / "v1.db"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE search_profiles (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            name              TEXT NOT NULL,
            keywords          TEXT NOT NULL,
            location          TEXT NOT NULL DEFAULT '',
            radius_km         INTEGER NOT NULL DEFAULT 0,
            sources           TEXT NOT NULL DEFAULT '[]',
            active            INTEGER NOT NULL DEFAULT 1,
            auto_send         INTEGER NOT NULL DEFAULT 0,
            poll_interval_min INTEGER NOT NULL DEFAULT 60,
            last_polled_at    TEXT,
            last_poll_error   TEXT,
            created_at        TEXT NOT NULL
        )
        """
    )
    con.execute(
        "INSERT INTO search_profiles (name, keywords, created_at) VALUES (?, ?, ?)",
        ("Python DE", "Python", "2026-07-01T10:00:00"),
    )
    con.commit()

    migrations.migrate(con)

    row = con.execute("SELECT * FROM search_profiles").fetchone()
    assert row["hard_tags"] == ""
    assert row["soft_preferences"] == ""
    assert row["strictness"] == 50
    assert row["keywords"] == "Python"  # existing data untouched
    assert (con.execute("PRAGMA user_version").fetchone()[0]
            == migrations.SCHEMA_VERSION)
    migrations.migrate(con)  # idempotent with the new columns present
    con.close()


def test_migrate_remaps_v1_zero_scores_to_the_new_floor(tmp_path):
    """v1 stored 0 = 'very bad fit'; v2 reserves 0 for hard violations."""
    path = tmp_path / "v1.db"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    migrations.migrate(con)  # full current schema, then rewind the stamp
    con.execute("PRAGMA user_version = 1")
    con.executemany(
        """
        INSERT INTO jobs (source, external_id, fetched_at, match_score)
        VALUES (?, ?, '2026-07-01T10:00:00', ?)
        """,
        [("stub", "old-zero", 0), ("stub", "old-fifty", 50),
         ("stub", "unscored", None)],
    )
    con.commit()

    migrations.migrate(con)

    scores = {r["external_id"]: r["match_score"]
              for r in con.execute("SELECT external_id, match_score FROM jobs")}
    assert scores == {"old-zero": 1, "old-fifty": 50, "unscored": None}

    # a v2 zero is a genuine violation signal and must survive re-migration
    con.execute("UPDATE jobs SET match_score=0 WHERE external_id='old-zero'")
    con.commit()
    migrations.migrate(con)
    assert con.execute(
        "SELECT match_score FROM jobs WHERE external_id='old-zero'"
    ).fetchone()[0] == 0
    con.close()


def test_migrate_adds_contact_columns_to_pre_v3_jobs(tmp_path):
    """A pre-v3 jobs table gains the contact/refnr columns with defaults."""
    path = tmp_path / "v2.db"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            external_id TEXT NOT NULL,
            fetched_at  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'new',
            match_score INTEGER,
            UNIQUE (source, external_id)
        )
        """
    )
    con.execute(
        "INSERT INTO jobs (source, external_id, fetched_at) VALUES (?, ?, ?)",
        ("stub", "j1", "2026-07-01T10:00:00"),
    )
    con.execute("PRAGMA user_version = 2")
    con.commit()

    migrations.migrate(con)

    row = con.execute("SELECT * FROM jobs").fetchone()
    for col in ("ansprechpartner", "contact_phone", "contact_strasse",
                "contact_plz_ort", "contact_source", "refnr"):
        assert row[col] == ""
    assert (con.execute("PRAGMA user_version").fetchone()[0]
            == migrations.SCHEMA_VERSION)
    migrations.migrate(con)  # idempotent with the new columns present
    con.close()


def test_migrate_derives_published_on_from_every_source_format(tmp_path):
    """A pre-v6 jobs table gains the freshness columns, and the derived ISO
    date is filled from whatever shape each board sent."""
    path = tmp_path / "v5.db"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE jobs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            source       TEXT NOT NULL,
            external_id  TEXT NOT NULL,
            published_at TEXT NOT NULL DEFAULT '',
            fetched_at   TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'new',
            match_score  INTEGER,
            UNIQUE (source, external_id)
        )
        """
    )
    rows = [
        ("arbeitsagentur", "a1", "2026-06-09", "2026-06-09"),
        ("arbeitnow", "n1", "1785897635", "2026-08-05"),
        ("jooble", "j1", "2026-07-13T00:00:00.0000000", "2026-07-13"),
        ("stub", "s1", "", ""),           # nothing to derive from
        ("stub", "s2", "irgendwann", ""),  # unreadable stays unknown
    ]
    for source, external_id, published_at, _ in rows:
        con.execute(
            "INSERT INTO jobs (source, external_id, published_at, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (source, external_id, published_at, "2026-08-06T10:00:00"),
        )
    con.execute("PRAGMA user_version = 5")
    con.commit()

    migrations.migrate(con)

    stored = {
        r["external_id"]: (r["published_at"], r["published_on"], r["liveness"],
                           r["liveness_checked_at"])
        for r in con.execute("SELECT * FROM jobs")
    }
    for source, external_id, published_at, expected in rows:
        raw, derived, liveness, checked = stored[external_id]
        assert raw == published_at, f"{source}: the board's own value is kept"
        assert derived == expected, f"{source}: derived ISO date"
        assert liveness == "" and checked == ""  # nothing observed yet
    assert (con.execute("PRAGMA user_version").fetchone()[0]
            == migrations.SCHEMA_VERSION)
    migrations.migrate(con)  # idempotent — the second pass finds nothing to fill
    assert con.execute(
        "SELECT published_on FROM jobs WHERE external_id='n1'"
    ).fetchone()[0] == "2026-08-05"
    con.close()


def test_migrate_never_overwrites_a_published_on_it_already_has(tmp_path):
    """The backfill fills blanks only: a date corrected by hand (or by a later
    parser) must survive every subsequent start."""
    path = tmp_path / "v6.db"
    con = db.connect(path)
    migrations.migrate(con)
    con.execute(
        "INSERT INTO jobs (source, external_id, published_at, published_on, "
        "fetched_at) VALUES ('stub', 'x', '1785897635', '2020-01-01', ?)",
        ("2026-08-06T10:00:00",),
    )
    con.commit()

    migrations.migrate(con)

    assert con.execute(
        "SELECT published_on FROM jobs WHERE external_id='x'"
    ).fetchone()[0] == "2020-01-01"
    con.close()


def test_migrate_adds_sending_test_to_pre_v4_drafts(tmp_path):
    """A pre-v4 drafts table gains sending_test defaulting to 0 — an
    existing draft must never look like an in-flight test send."""
    path = tmp_path / "v3.db"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE drafts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id     INTEGER NOT NULL,
            status     TEXT NOT NULL DEFAULT 'generating',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        "INSERT INTO drafts (job_id, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (1, "ready", "2026-07-01T10:00:00", "2026-07-01T10:00:00"),
    )
    con.execute("PRAGMA user_version = 3")
    con.commit()

    migrations.migrate(con)

    assert con.execute("SELECT * FROM drafts").fetchone()["sending_test"] == 0
    assert (con.execute("PRAGMA user_version").fetchone()[0]
            == migrations.SCHEMA_VERSION)
    migrations.migrate(con)  # idempotent with the new column present
    con.close()


def test_bootstrap_imports_legacy_db(data_dir, monkeypatch):
    legacy_path = data_dir / "old_bewerbungen.db"
    make_legacy_db(legacy_path, LEGACY_ROWS)
    monkeypatch.setattr(db, "_find_legacy_db", lambda: legacy_path)

    result = db.bootstrap()

    assert config.DB_PATH.exists()
    assert result is not None and result.ok and result.path is not None
    recovery = sqlite3.connect(result.path)
    recovery_tables = {
        row[0]
        for row in recovery.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "jobs" not in recovery_tables
    assert recovery.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 2
    recovery.close()
    with db.db() as con:
        assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 2
    # legacy file untouched (still pre-migration: no jobs table)
    legacy_con = sqlite3.connect(legacy_path)
    names = {r[0] for r in
             legacy_con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "jobs" not in names
    legacy_con.close()


def test_bootstrap_without_legacy_starts_empty(data_dir, monkeypatch):
    monkeypatch.setattr(db, "_find_legacy_db", lambda: None)
    assert db.bootstrap() is None
    with db.db() as con:
        assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 0


def test_bootstrap_refuses_to_migrate_without_a_verified_recovery_point(
    data_dir, monkeypatch
):
    make_legacy_db(config.DB_PATH, LEGACY_ROWS)
    migrated = False

    def mark_migration(_con):
        nonlocal migrated
        migrated = True

    monkeypatch.setattr(migrations, "migrate", mark_migration)
    monkeypatch.setattr(
        backup,
        "run_startup_backup",
        lambda: backup.BackupResult(error="Backup failed: injected failure"),
    )

    with pytest.raises(RuntimeError, match="injected failure"):
        db.bootstrap()

    assert not migrated
    legacy = sqlite3.connect(config.DB_PATH)
    assert "jobs" not in _tables(legacy)
    assert legacy.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 2
    legacy.close()


def test_bootstrap_imports_a_legacy_path_with_uri_delimiters(data_dir, monkeypatch):
    legacy_path = data_dir / "legacy?#candidate.db"
    make_legacy_db(legacy_path, LEGACY_ROWS)
    monkeypatch.setattr(db, "_find_legacy_db", lambda: legacy_path)

    result = db.bootstrap()

    assert result is not None and result.ok
    with db.db() as con:
        assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 2


def test_failed_migration_keeps_recovery_snapshot_and_retries_safely(
    data_dir, monkeypatch
):
    make_legacy_db(config.DB_PATH, LEGACY_ROWS)
    original = migrations._ensure_search_profile_columns

    def fail_after_additive_ddl(_con):
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(migrations, "_ensure_search_profile_columns", fail_after_additive_ddl)

    with pytest.raises(RuntimeError, match="injected migration failure"):
        db.bootstrap()

    first_backup = config.BACKUP_DIR / backup._list_backups(backup._backup_key())[0]
    recovery = sqlite3.connect(first_backup)
    assert "jobs" not in _tables(recovery)
    assert recovery.execute("PRAGMA user_version").fetchone()[0] == 0
    assert recovery.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 2
    recovery.close()

    monkeypatch.setattr(migrations, "_ensure_search_profile_columns", original)
    result = db.bootstrap()

    assert result is not None and result.ok
    migrated = sqlite3.connect(config.DB_PATH)
    assert migrated.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
    assert migrated.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 2
    migrated.close()


def test_migrate_adds_the_source_fact_columns_to_pre_v7_jobs(tmp_path):
    """A pre-v7 jobs table gains the columns for the facts the boards state —
    work address, pay range, Arbeitnehmerüberlassung — with defaults, and the
    rows already stored keep everything they had."""
    path = tmp_path / "v6.db"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            external_id TEXT NOT NULL,
            company     TEXT NOT NULL DEFAULT '',
            fetched_at  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'new',
            UNIQUE (source, external_id)
        )
        """
    )
    con.execute(
        "INSERT INTO jobs (source, external_id, company, fetched_at) "
        "VALUES (?, ?, ?, ?)",
        ("arbeitsagentur", "10001-1-S", "Beispiel GmbH", "2026-08-01T10:00:00"),
    )
    con.execute("PRAGMA user_version = 6")
    con.commit()

    migrations.migrate(con)

    row = con.execute("SELECT * FROM jobs").fetchone()
    assert row["company"] == "Beispiel GmbH"
    for col in ("work_strasse", "work_plz_ort", "salary_from", "salary_to",
                "salary_period"):
        assert row[col] == ""
    assert row["temp_agency"] == 0
    assert (con.execute("PRAGMA user_version").fetchone()[0]
            == migrations.SCHEMA_VERSION)
    migrations.migrate(con)  # idempotent with the new columns present
    con.close()


def test_migrate_adds_the_reading_columns_to_pre_v8_jobs(tmp_path):
    """A pre-v8 jobs table gains `bookmarked_at` and `opened_at`, both empty,
    without disturbing the rows already stored — nothing he has is marked or
    read until he marks or reads it."""
    path = tmp_path / "v7.db"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            external_id TEXT NOT NULL,
            company     TEXT NOT NULL DEFAULT '',
            fetched_at  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'new',
            UNIQUE (source, external_id)
        )
        """
    )
    con.execute(
        "INSERT INTO jobs (source, external_id, company, fetched_at) "
        "VALUES (?, ?, ?, ?)",
        ("arbeitsagentur", "10001-1-S", "Beispiel GmbH", "2026-08-01T10:00:00"),
    )
    con.execute("PRAGMA user_version = 7")
    con.commit()

    migrations.migrate(con)

    row = con.execute("SELECT * FROM jobs").fetchone()
    assert row["company"] == "Beispiel GmbH"
    assert row["bookmarked_at"] == ""
    assert row["opened_at"] == ""
    assert (con.execute("PRAGMA user_version").fetchone()[0]
            == migrations.SCHEMA_VERSION)
    migrations.migrate(con)  # idempotent with the new columns present
    con.close()


def _pre_v10_db(path):
    """A jobs+drafts pair shaped like the schema just before v10."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            external_id TEXT NOT NULL,
            company     TEXT NOT NULL DEFAULT '',
            fetched_at  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'new',
            opened_at   TEXT NOT NULL DEFAULT '',
            UNIQUE (source, external_id)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE drafts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id     INTEGER NOT NULL,
            status     TEXT NOT NULL DEFAULT 'ready',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    return con


def _add_job(con, external_id, status, opened_at=""):
    cur = con.execute(
        "INSERT INTO jobs (source, external_id, company, fetched_at, status, "
        "opened_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("arbeitsagentur", external_id, "Beispiel GmbH", "2026-08-01T10:00:00",
         status, opened_at),
    )
    return cur.lastrowid


def test_migrate_restates_an_open_form_as_a_moment(tmp_path):
    """The postings whose form was already open keep their place in the working
    list and gain the timestamp the app now reads.

    Their age comes from evidence — the newest draft, else when he opened the
    posting — and a posting that left neither says so rather than being handed
    an invented age, which would sort it among applications started this
    minute."""
    con = _pre_v10_db(tmp_path / "v9.db")
    from_draft = _add_job(con, "10001-1-S", "portal", "2026-08-14T09:00:00")
    con.execute(
        "INSERT INTO drafts (job_id, status, created_at, updated_at) "
        "VALUES (?, 'ready', ?, ?)",
        (from_draft, "2026-08-14T15:00:00", "2026-08-14T16:42:46"),
    )
    from_opened = _add_job(con, "10002-1-S", "portal", "2026-08-14T10:58:26")
    no_evidence = _add_job(con, "10003-1-S", "portal")
    untouched = _add_job(con, "10004-1-S", "new", "2026-08-14T11:00:00")
    con.execute("PRAGMA user_version = 9")
    con.commit()

    migrations.migrate(con)

    rows = {r["id"]: r for r in con.execute("SELECT * FROM jobs")}
    # the newest draft is the best evidence of when the form was opened
    assert rows[from_draft]["form_opened_at"] == "2026-08-14T16:42:46"
    # no draft — when he opened the posting is the next best thing
    assert rows[from_opened]["form_opened_at"] == "2026-08-14T10:58:26"
    # neither: named as unknown, never a clock value
    assert rows[no_evidence]["form_opened_at"] == constants.FORM_OPENED_UNKNOWN
    # every one of them is back in the working list
    assert [rows[i]["status"] for i in (from_draft, from_opened, no_evidence)] \
        == ["new", "new", "new"]
    # a posting that was never at a form is left completely alone
    assert rows[untouched]["form_opened_at"] == ""
    assert rows[untouched]["status"] == "new"
    # the new columns start empty: nothing is staged until a build stages it
    assert rows[from_draft]["upload_path"] == ""
    assert rows[from_draft]["mappe_kind"] == ""
    assert (con.execute("PRAGMA user_version").fetchone()[0]
            == migrations.SCHEMA_VERSION)

    migrations.migrate(con)
    after = {r["id"]: r for r in con.execute("SELECT * FROM jobs")}
    # a second run must not re-stamp: the moments are evidence, not defaults
    assert all(after[i]["form_opened_at"] == rows[i]["form_opened_at"]
               for i in rows)
    con.close()


def test_migrate_leaves_a_fresh_database_with_the_form_flow_columns(tmp_path):
    """A database created from scratch has them from the canonical CREATE
    TABLE, not from an ALTER — the two definitions must not drift."""
    con = sqlite3.connect(tmp_path / "fresh.db")
    con.row_factory = sqlite3.Row
    migrations.migrate(con)
    cols = [row[1] for row in con.execute("PRAGMA table_info(jobs)")]
    assert "form_opened_at" in cols
    assert "upload_path" in cols
    assert "mappe_kind" in cols
    con.close()


def test_migrate_points_an_application_at_the_mappe_that_was_built_for_it(tmp_path,
                                                                          monkeypatch):
    """Two writers recorded a form application and only one passed `dokument`,
    so 13 of his 35 Online-Portal ledger rows say no document exists while the
    PDF sits under output/job_<id>/. One recorder makes that impossible going
    forward; this fills in what the disagreement already cost.

    The file has to still be there: an entry naming a path that is gone is
    worse than one that admits it has nothing, because he would click it."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    con = sqlite3.connect(tmp_path / "fresh.db")
    con.row_factory = sqlite3.Row
    migrations.migrate(con)

    real = tmp_path / "output" / "job_1" / "Bewerbung.pdf"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"%PDF-1.4")
    gone = tmp_path / "output" / "job_2" / "Weg.pdf"

    ids = {}
    for key, path in (("has_file", real), ("no_file", gone)):
        bewerbung_id = db.add_bewerbung(con, {
            "firma": f"Firma {key}", "kanal": "Online-Portal",
            "status": "Gesendet", "dokument": ""})
        job_id = db.insert_job_if_new(con, {
            "source": "stub", "external_id": key, "company": f"Firma {key}",
            "title": "Entwickler", "url": "https://x.example/1"})
        db.set_job_status(con, job_id, "applied", bewerbung_id=bewerbung_id)
        db.upsert_draft(con, job_id, {"status": "sent", "pdf_path": str(path)})
        ids[key] = bewerbung_id
    con.commit()

    migrations.migrate(con)

    assert db.get_bewerbung(con, ids["has_file"])["dokument"] == str(real)
    assert db.get_bewerbung(con, ids["no_file"])["dokument"] == ""
    # and it never overwrites a pointer the app already has: this fills
    # blanks, it does not decide what a sent application was sent with
    con.execute("UPDATE bewerbungen SET dokument='/hand/gewaehlt.pdf' WHERE id=?",
                (ids["has_file"],))
    con.commit()
    migrations.migrate(con)
    assert db.get_bewerbung(con, ids["has_file"])["dokument"] == "/hand/gewaehlt.pdf"
    con.close()


def test_migrate_files_letters_whose_application_is_already_recorded(tmp_path,
                                                                    monkeypatch):
    """Recording a form application used to leave its letter at 'ready', so it
    waited in the Postausgang for ever. On his real database twelve of the
    seventeen waiting letters were of that kind — each offering to be e-mailed
    to a company he had applied to hours before.

    Only where the POSTING carries the application. A letter at a company
    reached through a DIFFERENT posting is left alone: the queue already warns
    on that row, and filing it would claim an employer holds a letter nobody
    ever sent."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    con = sqlite3.connect(tmp_path / "fresh.db")
    con.row_factory = sqlite3.Row
    migrations.migrate(con)

    bewerbung_id = db.add_bewerbung(con, {
        "firma": "Formular GmbH", "kanal": "Online-Portal",
        "status": "Gesendet"})
    own = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "own", "company": "Formular GmbH",
        "title": "Entwickler", "url": "https://x.example/1"})
    db.set_job_status(con, own, "applied", bewerbung_id=bewerbung_id)
    db.upsert_draft(con, own, {"status": "ready", "betreff": "Bewerbung"})
    # same company, a second posting: refused by the duplicate gate, so it
    # points at the application without being the one that carried the letter
    other = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "other", "company": "Formular GmbH",
        "title": "Entwickler II", "url": "https://x.example/2"})
    con.execute("UPDATE jobs SET status='duplicate', duplicate_of=? WHERE id=?",
                (bewerbung_id, other))
    db.upsert_draft(con, other, {"status": "ready", "betreff": "Bewerbung II"})
    # and one that is simply waiting, at a company with no application at all
    waiting = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "waiting", "company": "Offen GmbH",
        "title": "Entwickler III", "url": "https://x.example/3"})
    db.upsert_draft(con, waiting, {"status": "ready", "betreff": "Bewerbung III"})
    con.commit()
    assert db.count_waiting_drafts(con) == 3
    # the state a v10 database is in when this build first opens it
    con.execute("PRAGMA user_version = 10")

    migrations.migrate(con)

    filed = db.get_draft_by_job(con, own)
    assert filed["status"] == "filed"
    assert filed["bewerbung_id"] == bewerbung_id
    assert db.get_draft_by_job(con, other)["status"] == "ready"
    assert db.get_draft_by_job(con, waiting)["status"] == "ready"
    assert db.count_waiting_drafts(con) == 2
    con.close()


def test_the_filing_backfill_skips_a_database_too_old_to_know_the_pairing(
        tmp_path, monkeypatch):
    """A migration must never RAISE on a shape it can simply skip: it runs at
    startup, before any screen, so an exception there is the whole app.

    The early return is the branch a fresh database can never exercise, so it
    is exercised here — with the two columns the pairing is derived from
    absent, which is what a database from before schema v6 looks like."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    con = sqlite3.connect(tmp_path / "ancient.db")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, company TEXT)")
    con.execute("CREATE TABLE drafts (id INTEGER PRIMARY KEY, job_id INTEGER, "
                "status TEXT)")
    con.execute("INSERT INTO drafts (job_id, status) VALUES (1, 'ready')")
    con.commit()

    migrations._file_letters_of_recorded_applications(con)

    assert con.execute("SELECT status FROM drafts").fetchone()[0] == "ready"
    con.close()


def test_the_filing_backfill_never_raises_on_a_dangling_reference(tmp_path,
                                                                  monkeypatch):
    """`drafts.bewerbung_id` is a foreign key and `PRAGMA foreign_keys` is ON,
    so one `jobs.bewerbung_id` pointing at a deleted row made the UPDATE raise
    IntegrityError out of `migrate()` — which runs at startup, before any
    screen, so it is not a broken screen but an app that cannot open."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    con = sqlite3.connect(tmp_path / "dangling.db")
    con.row_factory = sqlite3.Row
    migrations.migrate(con)
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "x", "company": "Firma",
        "title": "Entwickler", "url": "https://x.example/1"})
    db.upsert_draft(con, job_id, {"status": "ready"})
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("UPDATE jobs SET bewerbung_id=999, status='applied' WHERE id=?",
                (job_id,))
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA user_version = 10")
    con.commit()

    migrations.migrate(con)   # must not raise

    assert db.get_draft_by_job(con, job_id)["status"] == "ready"
    con.close()


def test_the_filing_backfill_governs_history_and_not_the_future(tmp_path,
                                                                monkeypatch):
    """'ready' is a state a draft comes BACK to. Run on every start this would
    stop healing history and start freezing letters he had deliberately
    restored or re-written, silently, at the next launch."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    con = sqlite3.connect(tmp_path / "again.db")
    con.row_factory = sqlite3.Row
    migrations.migrate(con)
    bewerbung_id = db.add_bewerbung(con, {"firma": "Firma", "kanal": "E-Mail",
                                          "status": "Gesendet"})
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "x", "company": "Firma",
        "title": "Entwickler", "url": "https://x.example/1"})
    db.set_job_status(con, job_id, "applied", bewerbung_id=bewerbung_id)
    db.upsert_draft(con, job_id, {"status": "ready"})
    con.commit()

    migrations.migrate(con)   # the next app start, already at this version

    assert db.get_draft_by_job(con, job_id)["status"] == "ready"
    con.close()


def test_the_backfill_files_the_letter_the_running_app_would_mean(tmp_path,
                                                                  monkeypatch):
    """`drafts.job_id` has no UNIQUE constraint. Filing all of a posting's
    letters would make the migration and `apply_record` — which files only
    `get_draft_by_job`, the newest — disagree about how many an application
    carried, and the undo would then hand several back."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    con = sqlite3.connect(tmp_path / "two.db")
    con.row_factory = sqlite3.Row
    migrations.migrate(con)
    bewerbung_id = db.add_bewerbung(con, {"firma": "Firma", "kanal": "E-Mail",
                                          "status": "Gesendet"})
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "x", "company": "Firma",
        "title": "Entwickler", "url": "https://x.example/1"})
    db.set_job_status(con, job_id, "applied", bewerbung_id=bewerbung_id)
    older = con.execute(
        "INSERT INTO drafts (job_id, status, created_at, updated_at) "
        "VALUES (?, 'ready', 't', 't')", (job_id,)).lastrowid
    newer = con.execute(
        "INSERT INTO drafts (job_id, status, created_at, updated_at) "
        "VALUES (?, 'ready', 't', 't')", (job_id,)).lastrowid
    con.execute("PRAGMA user_version = 10")
    con.commit()

    migrations.migrate(con)

    assert db.get_draft(con, newer)["status"] == "filed"
    assert db.get_draft(con, older)["status"] == "ready"
    # …and it stamps the moment, so the Postausgang does not print the
    # letter's last edit as the moment the Mappe was handed over
    assert db.get_draft(con, newer)["updated_at"] != "t"
    con.close()


def test_migrate_adds_the_reply_columns_to_a_pre_v12_email_log(tmp_path):
    """A pre-v12 email_log gains body_text (default '') and job_id (default
    NULL) — existing outbound rows must read as having no stored body and no
    posting link, not as anything else."""
    path = tmp_path / "v11.db"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE email_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            direction        TEXT NOT NULL,
            gmail_message_id TEXT UNIQUE,
            gmail_thread_id  TEXT NOT NULL DEFAULT '',
            from_addr        TEXT NOT NULL DEFAULT '',
            to_addr          TEXT NOT NULL DEFAULT '',
            subject          TEXT NOT NULL DEFAULT '',
            snippet          TEXT NOT NULL DEFAULT '',
            internal_date    TEXT NOT NULL DEFAULT '',
            draft_id         INTEGER,
            bewerbung_id     INTEGER,
            matched_by       TEXT NOT NULL DEFAULT '',
            classification   TEXT NOT NULL DEFAULT '',
            classified_by    TEXT NOT NULL DEFAULT '',
            needs_review     INTEGER NOT NULL DEFAULT 0,
            created_at       TEXT NOT NULL
        )
        """
    )
    con.execute(
        "INSERT INTO email_log (direction, gmail_message_id, gmail_thread_id, "
        "created_at) VALUES ('outbound', 'm-1', 't-1', '2026-08-01T10:00:00')"
    )
    con.execute("PRAGMA user_version = 11")
    con.commit()

    migrations.migrate(con)

    row = con.execute("SELECT * FROM email_log").fetchone()
    assert row["body_text"] == ""
    assert row["job_id"] is None
    index_names = {r["name"] for r in con.execute("PRAGMA index_list(email_log)")}
    assert "idx_email_log_thread" in index_names
    assert (con.execute("PRAGMA user_version").fetchone()[0]
            == migrations.SCHEMA_VERSION)
    migrations.migrate(con)  # idempotent with the new columns present
    con.close()


def test_migrate_leaves_a_fresh_database_with_the_reply_columns(tmp_path):
    """The canonical CREATE TABLE and the ALTER path must agree — a fresh
    database drifting from an upgraded one is how one machine works and the
    next install fails."""
    con = sqlite3.connect(tmp_path / "fresh.db")
    con.row_factory = sqlite3.Row
    migrations.migrate(con)
    columns = {row["name"] for row in con.execute("PRAGMA table_info(email_log)")}
    assert {"body_text", "job_id"} <= columns
    index_names = {r["name"] for r in con.execute("PRAGMA index_list(email_log)")}
    assert "idx_email_log_thread" in index_names
    con.close()


def _corpus_hash(con) -> str:
    import hashlib
    rows = con.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    return hashlib.md5(repr([tuple(r) for r in rows]).encode()).hexdigest()


def test_an_upgrade_from_v12_never_touches_a_posting(data_dir):
    """Strictly additive. On his corpus a v12 run must leave 1167 postings
    byte-identical, which is the promise the first start after a merge has to
    keep. Pinned to the CURRENT version rather than to a literal, because the
    property being asserted is "no upgrade rewrites the corpus" — every later
    slice inherits it instead of editing this line."""
    from jobdeck import db, migrations

    con = db.connect()
    migrations.migrate(con)
    for n in range(5):
        db.insert_job_if_new(con, {
            "source": "stub", "external_id": f"e{n}", "title": "Entwickler",
            "company": f"Firma {n}", "url": "https://example.invalid/1",
        })
    con.commit()
    # Settled first: the published-on and document backfills run on EVERY
    # start by design and are self-healing, so a corpus that has never seen
    # them is not the state his database is in. Measured on his real one, the
    # v12 → v13 run left all 1167 postings byte-identical.
    migrations.migrate(con)
    con.execute("PRAGMA user_version = 12")
    con.execute("DROP TABLE IF EXISTS hidden_companies")
    con.commit()
    # tuple(), because `repr(sqlite3.Row)` is a memory address — a hash over
    # those compares two allocations, not two corpora.
    before = _corpus_hash(con)

    migrations.migrate(con)

    assert con.execute("PRAGMA user_version").fetchone()[0] == (
        migrations.SCHEMA_VERSION
    )
    assert _corpus_hash(con) == before, "the upgrade touched a posting"
    assert con.execute("SELECT COUNT(*) FROM hidden_companies").fetchone()[0] == 0
    # …and running it again is a no-op
    migrations.migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == (
        migrations.SCHEMA_VERSION
    )
    assert _corpus_hash(con) == before
    con.close()


def test_the_hidden_table_hands_out_a_number_that_never_repeats(data_dir):
    """AUTOINCREMENT, and it is load-bearing: the page signature rests on
    MAX(id), and a plain rowid is reassigned after a DELETE."""
    from jobdeck import db, migrations

    con = db.connect()
    migrations.migrate(con)
    first = db.hide_company(con, "Alpha GmbH")
    db.hide_company(con, "Beta GmbH")
    highest = con.execute("SELECT MAX(id) FROM hidden_companies").fetchone()[0]

    db.unhide_company(con, first)
    con.execute("DELETE FROM hidden_companies WHERE company_key<>''")
    con.commit()
    db.hide_company(con, "Gamma GmbH")

    assert con.execute(
        "SELECT MAX(id) FROM hidden_companies").fetchone()[0] > highest
    con.close()


# --------------------------------------------------------------------------
# v14 — every application in the ledger gets the attempt that made it
# --------------------------------------------------------------------------
def _seed_ledger_for_attempts(con):
    """Three applications: two with a linked posting, one recorded by hand."""
    from jobdeck import db

    by_post = db.add_bewerbung(con, {
        "gesendet_am": "2026-08-10", "firma": "Beispiel GmbH", "kanal": "Online-Portal",
        "status": "Gesendet"})
    second = db.add_bewerbung(con, {
        "gesendet_am": "2026-07-01", "firma": "Müller & Co", "kanal": "E-Mail",
        "status": "Absage"})
    by_hand = db.add_bewerbung(con, {
        "gesendet_am": "2026-06-12", "firma": "Handarbeit GmbH",
        "kanal": "Online-Portal", "status": "Absage"})
    jobs = {}
    for key, (bew, title) in {
        "beispiel": (by_post, "AI & Backend Engineer"),
        "mueller": (second, "Python Entwickler (m/w/d)"),
    }.items():
        job_id = db.insert_job_if_new(con, {
            "source": "stub", "external_id": key, "title": title,
            "company": "Beispiel GmbH" if key == "beispiel" else "Müller & Co",
            "url": "https://example.invalid/1"})
        db.set_job_status(con, job_id, "applied", bewerbung_id=bew)
        jobs[key] = job_id
    con.commit()
    return by_post, second, by_hand, jobs


def test_v14_gives_every_recorded_application_an_attempt(data_dir):
    from jobdeck import db, migrations

    con = db.connect()
    migrations.migrate(con)
    by_post, second, by_hand, jobs = _seed_ledger_for_attempts(con)
    con.execute("PRAGMA user_version = 13")
    con.execute("DELETE FROM application_attempts")
    con.commit()

    migrations.migrate(con)

    rows = {
        row["bewerbung_id"]: dict(row)
        for row in con.execute("SELECT * FROM application_attempts")
    }
    assert set(rows) == {by_post, second, by_hand}
    assert all(row["state"] == "recorded" for row in rows.values())
    # The position is the posting's title, and it is the ONLY place it exists.
    assert rows[by_post]["position"] == "AI & Backend Engineer"
    assert rows[by_post]["idempotency_key"] == f"job:{jobs['beispiel']}"
    assert rows[by_post]["job_id"] == jobs["beispiel"]
    assert rows[by_post]["channel"] == "Online-Portal"
    # Recorded by hand: no posting, so no position. Empty means UNKNOWN.
    assert rows[by_hand]["position"] == ""
    assert rows[by_hand]["job_id"] is None
    assert rows[by_hand]["idempotency_key"] == f"bewerbung:{by_hand}"
    con.close()


def test_v14_folds_the_company_key_exactly_like_the_gate(data_dir):
    """A second folding rule is how a filter starts disagreeing with the gate
    it mirrors, so the stored key must equal `dedupe.norm` and not merely
    resemble it."""
    from jobdeck import db, migrations
    from jobdeck.dedupe import norm

    con = db.connect()
    migrations.migrate(con)
    bew = db.add_bewerbung(con, {
        "gesendet_am": "2026-08-10", "firma": "Beispiel® GmbH",
        "kanal": "E-Mail", "status": "Gesendet"})
    con.execute("PRAGMA user_version = 13")
    con.execute("DELETE FROM application_attempts")
    con.commit()

    migrations.migrate(con)

    stored = con.execute(
        "SELECT company_key, company FROM application_attempts WHERE bewerbung_id=?",
        (bew,)).fetchone()
    assert stored["company_key"] == norm("Beispiel® GmbH") == "beispiel gmbh"
    # The readable spelling survives too — a screen cannot show the key.
    assert stored["company"] == "Beispiel® GmbH"
    con.close()


def test_v14_runs_once_and_never_resurrects_a_released_attempt(data_dir):
    """Version-gated on purpose. A backfill on every start would re-create the
    row for an attempt deliberately released and fight the live writers for
    its key."""
    from jobdeck import db, migrations

    con = db.connect()
    migrations.migrate(con)
    by_post, _second, _by_hand, _jobs = _seed_ledger_for_attempts(con)
    con.execute("PRAGMA user_version = 13")
    con.execute("DELETE FROM application_attempts")
    con.commit()
    migrations.migrate(con)
    con.execute(
        "UPDATE application_attempts SET state='released' WHERE bewerbung_id=?",
        (by_post,))
    con.commit()

    migrations.migrate(con)   # a later ordinary start

    assert con.execute(
        "SELECT state FROM application_attempts WHERE bewerbung_id=?",
        (by_post,)).fetchone()["state"] == "released"
    assert con.execute(
        "SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 3
    con.close()


def test_v13_becomes_v14_without_touching_a_posting_or_the_ledger(data_dir):
    """Additive: one table, and every write lands in it. The legacy ledger
    keeps its exact shape, which is what makes the rollback "stop reading the
    new table" instead of a migration back."""
    from jobdeck import db, migrations

    con = db.connect()
    migrations.migrate(con)
    _seed_ledger_for_attempts(con)
    con.execute("PRAGMA user_version = 13")
    con.execute("DROP TABLE IF EXISTS application_attempts")
    con.commit()
    before = _corpus_hash(con)
    ledger_before = [tuple(r) for r in con.execute("SELECT * FROM bewerbungen")]
    columns_before = [r[1] for r in con.execute("PRAGMA table_info(bewerbungen)")]

    migrations.migrate(con)

    # The current version, not the literal 14: the property under test is
    # "an upgrade never rewrites the corpus", which every later slice
    # inherits — see the v12 test for the same reasoning.
    assert con.execute("PRAGMA user_version").fetchone()[0] == (
        migrations.SCHEMA_VERSION)
    assert _corpus_hash(con) == before, "the upgrade touched a posting"
    assert [tuple(r) for r in con.execute("SELECT * FROM bewerbungen")] == (
        ledger_before
    )
    assert [r[1] for r in con.execute("PRAGMA table_info(bewerbungen)")] == (
        columns_before
    ), "the legacy ledger gained a column"
    con.close()


def test_v14_survives_a_pre_v7_jobs_table_without_the_link_column(tmp_path):
    """A jobs table from before the link column exists in the wild only as the
    partial shape the earlier migration tests build. Then no application HAS a
    linked posting, so every attempt is keyed by its ledger row with an
    unknown position — the truth for such a database, not a fallback."""
    path = tmp_path / "v6.db"
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            external_id TEXT NOT NULL,
            company     TEXT NOT NULL DEFAULT '',
            fetched_at  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'new',
            UNIQUE (source, external_id)
        );
        CREATE TABLE bewerbungen (
            id INTEGER PRIMARY KEY AUTOINCREMENT, gesendet_am TEXT, firma TEXT,
            ansprechpartner TEXT, strasse TEXT, plz_ort TEXT, kanal TEXT,
            status TEXT, notiz TEXT, created_at TEXT
        );
        INSERT INTO bewerbungen (gesendet_am, firma, kanal, status, created_at)
             VALUES ('2026-06-12', 'Alt GmbH', 'E-Mail', 'Absage', '2026-06-12');
        """
    )
    con.execute("PRAGMA user_version = 6")
    con.commit()

    migrations.migrate(con)

    row = con.execute("SELECT * FROM application_attempts").fetchone()
    assert row["idempotency_key"] == "bewerbung:1"
    assert row["position"] == ""
    assert row["job_id"] is None
    assert row["company_key"] == "alt gmbh"
    con.close()


def _legacy_register_row(con, fact="Django & DRF", binding="Praktikum"):
    """A register row exactly as schema v9 wrote it — no v15 columns."""
    con.execute(
        "INSERT INTO claims (fact, binding, terms, sort_order, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (fact, binding, "Django, DRF", 1, "2026-07-01 09:00:00",
         "2026-07-01 09:00:00"),
    )
    con.commit()


def _drop_v15_columns(con):
    """Rebuild `claims` in its pre-v15 shape, preserving the rows.

    SQLite before 3.35 cannot DROP COLUMN and the shipped table is small, so
    the test recreates it the way v9 did rather than assuming the interpreter's
    SQLite is new enough to undo the ALTERs.
    """
    rows = con.execute(
        "SELECT fact, binding, terms, sort_order, created_at, updated_at "
        "FROM claims ORDER BY id").fetchall()
    con.execute("DROP TABLE claims")
    con.execute(
        """
        CREATE TABLE claims (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            fact       TEXT NOT NULL,
            binding    TEXT NOT NULL DEFAULT '',
            terms      TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    con.executemany(
        "INSERT INTO claims (fact, binding, terms, sort_order, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        [tuple(r) for r in rows],
    )
    con.commit()


def test_v15_reads_an_existing_register_row_as_his_own_confirmed_skill(data_dir):
    """The defaults must state what a pre-v15 row already meant.

    Before v15 the register held only competences and the only way in was the
    user typing one. So an existing row is a CONFIRMED skill he wrote himself,
    and `confirmed_at` is the day he wrote it — not the day of the upgrade,
    which would date his confirmation to a migration he never ran by hand.
    """
    from jobdeck import db, migrations

    con = db.connect()
    migrations.migrate(con)
    _legacy_register_row(con)
    _drop_v15_columns(con)
    con.execute("PRAGMA user_version = 14")
    con.commit()

    migrations.migrate(con)

    row = con.execute("SELECT * FROM claims").fetchone()
    assert (con.execute("PRAGMA user_version").fetchone()[0]
            == migrations.SCHEMA_VERSION)
    assert row["kind"] == "skill"
    assert row["state"] == "confirmed"
    assert row["source"] == "user"
    assert row["source_ref"] == ""
    assert row["supersedes_id"] is None
    assert row["confirmed_at"] == "2026-07-01 09:00:00"
    assert row["fact"] == "Django & DRF" and row["binding"] == "Praktikum"
    con.close()


def test_v14_becomes_v15_without_touching_a_posting_or_the_ledger(data_dir):
    """Additive: six columns on one table we own. The legacy ledger keeps its
    exact shape, which is what makes the rollback "stop reading the new
    columns" instead of a migration back."""
    from jobdeck import db, migrations

    con = db.connect()
    migrations.migrate(con)
    _seed_ledger_for_attempts(con)
    _legacy_register_row(con)
    _drop_v15_columns(con)
    con.execute("PRAGMA user_version = 14")
    con.commit()
    before = _corpus_hash(con)
    ledger_before = [tuple(r) for r in con.execute("SELECT * FROM bewerbungen")]
    columns_before = [r[1] for r in con.execute("PRAGMA table_info(bewerbungen)")]

    migrations.migrate(con)

    assert _corpus_hash(con) == before, "the upgrade touched a posting"
    assert [tuple(r) for r in con.execute("SELECT * FROM bewerbungen")] == (
        ledger_before
    )
    assert [r[1] for r in con.execute("PRAGMA table_info(bewerbungen)")] == (
        columns_before
    ), "the legacy ledger gained a column"
    con.close()


def test_v15_backfills_the_confirmation_date_once_and_never_again(data_dir):
    """The guard runs on every start; the backfill must not.

    `confirmed_at` is a value the user can change later by re-confirming a
    corrected fact. A backfill that ran on every start would drag it back to
    `created_at` at the next launch — the same failure the v11 letter backfill
    had to be version-gated against.
    """
    from jobdeck import db, migrations

    con = db.connect()
    migrations.migrate(con)
    _legacy_register_row(con)
    _drop_v15_columns(con)
    con.execute("PRAGMA user_version = 14")
    con.commit()
    migrations.migrate(con)

    # Emptied deliberately: with `confirmed_at` set, the UPDATE's own WHERE
    # would protect the row and the test could not tell "inside the guard"
    # from "outside it". Empty is the one value the backfill WOULD rewrite.
    con.execute("UPDATE claims SET confirmed_at=''")
    con.commit()
    migrations.migrate(con)

    assert con.execute("SELECT confirmed_at FROM claims").fetchone()[0] == "", (
        "the backfill ran again on a later start")
    con.close()


# ---------------------------------------------------------------------------
# v16 — application_documents: what one build produced for one posting
# ---------------------------------------------------------------------------

def test_v15_becomes_v16_without_touching_a_posting_or_the_ledger(data_dir):
    """The table is additive. A v15 database carries every posting, draft and
    ledger row across unchanged, and the new table starts empty — nothing is
    derived from the disk at upgrade time."""
    con = db.connect()
    migrations.migrate(con)
    job_id = db.insert_job_if_new(con, {
        "source": "arbeitnow", "external_id": "d-1", "title": "Dev",
        "company": "Alpha GmbH", "description": "x"})
    db.upsert_draft(con, job_id, {"status": "ready", "pdf_path": "/x/y.pdf"})
    before = (con.execute("SELECT * FROM jobs").fetchall(),
              con.execute("SELECT * FROM drafts").fetchall())
    con.execute("DROP TABLE application_documents")
    con.execute("PRAGMA user_version = 15")
    con.commit()

    migrations.migrate(con)

    assert (con.execute("PRAGMA user_version").fetchone()[0]
            == migrations.SCHEMA_VERSION)
    assert con.execute("SELECT COUNT(*) FROM application_documents").fetchone()[0] == 0
    assert (con.execute("SELECT * FROM jobs").fetchall(),
            con.execute("SELECT * FROM drafts").fetchall()) == before
    con.close()


def test_v16_migration_is_repeatable(data_dir):
    con = db.connect()
    migrations.migrate(con)
    migrations.migrate(con)
    cols = [r[1] for r in con.execute("PRAGMA table_info(application_documents)")]
    assert {"job_id", "kind", "path", "staged_path", "sha256", "bytes",
            "pages", "built_at"} <= set(cols)
    con.close()


# ---------------------------------------------------------------------------
# v17 — drafts.profil: the CV's profile line written for one posting
# ---------------------------------------------------------------------------

def test_v16_becomes_v17_with_an_empty_profile_line_on_every_existing_draft(data_dir):
    """Additive. An existing draft gains profil = '', which is what it
    rendered all along (the template's fixed line); every other column of
    the row is carried across unchanged."""
    con = db.connect()
    migrations.migrate(con)
    job_id = db.insert_job_if_new(con, {
        "source": "arbeitnow", "external_id": "d-1", "title": "Dev",
        "company": "Alpha GmbH", "description": "x"})
    db.upsert_draft(con, job_id, {"status": "ready",
                                  "anschreiben_body": "Anrede,\n\nText."})
    # a v16 table: the column does not exist yet
    con.execute("ALTER TABLE drafts DROP COLUMN profil")
    con.execute("PRAGMA user_version = 16")
    con.commit()
    before = dict(con.execute("SELECT * FROM drafts").fetchone())
    assert "profil" not in before

    migrations.migrate(con)

    # the literal, not the constant: a test comparing the constant with
    # itself would stay green with the bump forgotten
    assert migrations.SCHEMA_VERSION == 17
    assert con.execute("PRAGMA user_version").fetchone()[0] == 17
    after = dict(con.execute("SELECT * FROM drafts").fetchone())
    assert after.pop("profil") == ""
    assert after == before
    # and the column is a draft field from here on: written, read, kept
    db.upsert_draft(con, job_id, {"profil": "Zwei Sätze."})
    assert db.get_draft_by_job(con, job_id)["profil"] == "Zwei Sätze."
    db.upsert_draft(con, job_id, {"status": "approved"})
    assert db.get_draft_by_job(con, job_id)["profil"] == "Zwei Sätze."
    migrations.migrate(con)  # repeatable
    con.close()
