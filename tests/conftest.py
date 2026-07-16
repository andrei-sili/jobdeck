import pytest

from jobdeck import config, db, migrations


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """Redirect the whole data directory into a temp folder."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jobdeck.db")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(config, "PROFILE_PATH", tmp_path / "profile.md")
    monkeypatch.setattr(config, "TOKEN_PATH", tmp_path / "token.json")
    monkeypatch.setattr(config, "CLIENT_SECRET_PATH", tmp_path / "client_secret.json")
    config.ensure_data_dirs()
    return tmp_path


@pytest.fixture()
def con(data_dir):
    """Migrated test database, WAL from the start like production.

    Must go through db.connect(): a plain sqlite3.connect would leave the
    file in delete journal mode, and the service's first concurrent
    connections would then race on the delete→WAL conversion — an
    exclusive-lock operation that fails fast ("database is locked")."""
    con = db.connect()
    migrations.migrate(con)
    yield con
    con.close()
