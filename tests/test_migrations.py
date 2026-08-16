import sqlite3

from jobdeck import config, constants, db, migrations


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

    db.bootstrap()

    assert config.DB_PATH.exists()
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
    db.bootstrap()
    with db.db() as con:
        assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 0


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
