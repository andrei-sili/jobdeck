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
    migrations.migrate(con)  # idempotent with the new columns present
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
