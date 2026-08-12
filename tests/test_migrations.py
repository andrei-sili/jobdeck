import sqlite3

from jobdeck import config, db, migrations


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
