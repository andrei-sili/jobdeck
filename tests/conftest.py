import sqlite3

import pytest

from jobdeck import config, migrations


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """Redirect the whole data directory into a temp folder."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jobdeck.db")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(config, "PROFILE_PATH", tmp_path / "profile.md")
    config.ensure_data_dirs()
    return tmp_path


@pytest.fixture()
def con(data_dir):
    """Migrated in-file test database with row access by name."""
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    migrations.migrate(con)
    yield con
    con.close()
