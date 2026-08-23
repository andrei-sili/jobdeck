import sqlite3

from jobdeck import backup, config


def _make_db(path, rows):
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE bewerbungen (id INTEGER PRIMARY KEY, firma TEXT)")
    con.executemany(
        "INSERT INTO bewerbungen (firma) VALUES (?)", [(f"Firma {i}",) for i in range(rows)]
    )
    con.commit()
    con.close()


def _backup_counts():
    key = backup._backup_key()
    return [
        backup._db_row_count(config.BACKUP_DIR / f) for f in backup._list_backups(key)
    ]


def test_normal_backup_no_warning(data_dir):
    _make_db(config.DB_PATH, 10)
    result = backup.run_startup_backup()
    assert result.ok and result.warning == "" and result.error == ""
    assert result.path is not None and result.path.exists()
    assert _backup_counts() == [10]


def test_empty_db_warns_and_is_copied_as_a_recovery_point(data_dir):
    _make_db(config.DB_PATH, 10)
    backup.run_startup_backup()
    _make_db(config.DB_PATH, 0)
    result = backup.run_startup_backup()
    assert result.ok and "10" in result.warning
    assert _backup_counts() == [10, 0]


def test_partial_loss_warns_every_start_and_best_backup_survives(data_dir):
    _make_db(config.DB_PATH, 100)
    assert backup.run_startup_backup().ok
    _make_db(config.DB_PATH, 10)
    warnings = [bool(backup.run_startup_backup().warning) for _ in range(14)]
    assert all(warnings)
    counts = _backup_counts()
    assert 100 in counts  # best backup never rotated out
    assert len(counts) <= backup.BACKUP_KEEP


def test_missing_db_does_not_crash(data_dir):
    _make_db(config.DB_PATH, 10)
    backup.run_startup_backup()
    config.DB_PATH.unlink()
    result = backup.run_startup_backup()
    assert not result.ok and "not a valid JobDeck database" in result.error


def test_stray_file_as_backup_dir_does_not_crash(data_dir, monkeypatch):
    _make_db(config.DB_PATH, 10)
    stray = data_dir / "stray"
    stray.write_text("not a directory")
    monkeypatch.setattr(config, "BACKUP_DIR", stray)
    result = backup.run_startup_backup()
    assert not result.ok and result.error.startswith("Backup failed:")


def test_corrupt_newest_backup_does_not_suppress_warning(data_dir):
    _make_db(config.DB_PATH, 20)
    backup.run_startup_backup()
    key = backup._backup_key()
    corrupt = config.BACKUP_DIR / f"jobdeck_{key}_2099-01-01_000000_000000.db"
    corrupt.write_bytes(b"garbage")
    _make_db(config.DB_PATH, 2)
    result = backup.run_startup_backup()
    assert result.ok and result.warning


def test_snapshot_failure_is_explicit_and_leaves_no_partial_file(
    data_dir, monkeypatch
):
    _make_db(config.DB_PATH, 3)

    def fail_after_writing(_source, destination, _expected):
        destination.write_bytes(b"partial")
        raise sqlite3.OperationalError("injected backup failure")

    monkeypatch.setattr(backup, "_copy_and_verify", fail_after_writing)

    result = backup.run_startup_backup()

    assert not result.ok
    assert "injected backup failure" in result.error
    assert list(config.BACKUP_DIR.glob("*.db")) == []


def test_verified_snapshot_preserves_schema_version_and_rows(data_dir):
    _make_db(config.DB_PATH, 4)
    con = sqlite3.connect(config.DB_PATH)
    con.execute("PRAGMA user_version = 7")
    con.commit()
    con.close()

    result = backup.run_startup_backup()

    assert result.ok and result.path is not None
    snapshot = sqlite3.connect(result.path)
    assert snapshot.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert snapshot.execute("PRAGMA user_version").fetchone()[0] == 7
    assert snapshot.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 4
    snapshot.close()


def test_backup_reads_a_database_path_with_uri_delimiters(data_dir, monkeypatch):
    special = data_dir / "candidate?#data.db"
    monkeypatch.setattr(config, "DB_PATH", special)
    _make_db(special, 3)

    result = backup.run_startup_backup()

    assert result.ok and result.path is not None
    assert backup._db_row_count(result.path) == 3
