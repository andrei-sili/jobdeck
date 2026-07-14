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
    assert backup.run_startup_backup() is None
    assert _backup_counts() == [10]


def test_empty_db_warns_and_is_not_copied(data_dir):
    _make_db(config.DB_PATH, 10)
    backup.run_startup_backup()
    _make_db(config.DB_PATH, 0)
    warning = backup.run_startup_backup()
    assert warning is not None and "10" in warning
    assert _backup_counts() == [10]  # the good backup remains alone


def test_partial_loss_warns_every_start_and_best_backup_survives(data_dir):
    _make_db(config.DB_PATH, 100)
    assert backup.run_startup_backup() is None
    _make_db(config.DB_PATH, 10)
    warnings = [backup.run_startup_backup() is not None for _ in range(14)]
    assert all(warnings)
    counts = _backup_counts()
    assert 100 in counts  # best backup never rotated out
    assert len(counts) <= backup.BACKUP_KEEP


def test_missing_db_does_not_crash(data_dir):
    _make_db(config.DB_PATH, 10)
    backup.run_startup_backup()
    config.DB_PATH.unlink()
    assert backup.run_startup_backup() is not None  # warning, no exception


def test_stray_file_as_backup_dir_does_not_crash(data_dir, monkeypatch):
    _make_db(config.DB_PATH, 10)
    stray = data_dir / "stray"
    stray.write_text("not a directory")
    monkeypatch.setattr(config, "BACKUP_DIR", stray)
    assert backup.run_startup_backup() is None  # swallowed, app must start


def test_corrupt_newest_backup_does_not_suppress_warning(data_dir):
    _make_db(config.DB_PATH, 20)
    backup.run_startup_backup()
    key = backup._backup_key()
    corrupt = config.BACKUP_DIR / f"jobdeck_{key}_2099-01-01_000000_000000.db"
    corrupt.write_bytes(b"garbage")
    _make_db(config.DB_PATH, 2)
    assert backup.run_startup_backup() is not None
