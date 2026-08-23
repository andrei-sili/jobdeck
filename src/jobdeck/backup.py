"""Verified, rotating SQLite backups for startup and manual maintenance.

Snapshots use SQLite's backup API and are validated before they can be treated
as recovery points. Backup failures never masquerade as success: callers get
an explicit result and can decide whether an operation (notably migration) is
safe to continue.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from jobdeck import config

BACKUP_KEEP = 10


@dataclass(frozen=True)
class BackupResult:
    """Outcome of one snapshot attempt.

    ``path`` is set only after the snapshot has passed validation. ``warning``
    reports data-loss or rotation concerns without invalidating that snapshot;
    ``error`` means no verified recovery point was created.
    """

    path: Path | None = None
    warning: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.path is not None and not self.error


@dataclass(frozen=True)
class _DatabaseState:
    user_version: int
    tables: frozenset[str]
    application_count: int


def _database_state(path: Path) -> _DatabaseState | None:
    """Return the integrity-relevant state of a JobDeck database."""
    if not path.is_file():
        return None
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        try:
            check = con.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                return None
            tables = frozenset(
                str(row[0])
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            )
            if "bewerbungen" not in tables:
                return None
            application_count = int(
                con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0]
            )
            user_version = int(con.execute("PRAGMA user_version").fetchone()[0])
            return _DatabaseState(user_version, tables, application_count)
        finally:
            con.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None


def _db_row_count(path: Path) -> int | None:
    """Compatibility helper used by backup lineage and regression tests."""
    state = _database_state(path)
    return None if state is None else state.application_count


def _backup_key() -> str:
    """Short identifier of the current database folder."""
    return hashlib.md5(str(config.DB_PATH.parent).encode("utf-8")).hexdigest()[:8]


def _list_backups(key: str) -> list[str]:
    """Backup filenames for the current lineage, oldest first."""
    try:
        return sorted(
            name
            for name in os.listdir(config.BACKUP_DIR)
            if name.startswith(f"jobdeck_{key}_") and name.endswith(".db")
        )
    except OSError:
        return []


def _scan(key: str) -> list[tuple[str, int]]:
    pairs: list[tuple[str, int]] = []
    for name in _list_backups(key):
        count = _db_row_count(config.BACKUP_DIR / name)
        if count is not None:
            pairs.append((name, count))
    return pairs


def _best_of(pairs: list[tuple[str, int]]) -> tuple[str | None, int | None]:
    best_name: str | None = None
    best_count: int | None = None
    for name, count in pairs:
        if best_count is None or count > best_count:
            best_name, best_count = name, count
    return best_name, best_count


def _data_loss_warning(
    current: int, best_name: str | None, best_count: int | None
) -> str:
    if best_count is None or best_count < 5 or current >= best_count / 2:
        return ""
    return (
        "The database holds far fewer applications than the best backup "
        f"({current} instead of {best_count}). If data was lost, restore from: "
        f"{config.BACKUP_DIR / (best_name or '')} — close the app and copy it to "
        f"{config.DB_PATH}. If you deleted entries on purpose, delete the old "
        f"backups in {config.BACKUP_DIR} to silence this warning."
    )


def _destination(key: str) -> Path:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
    destination = config.BACKUP_DIR / f"jobdeck_{key}_{stamp}.db"
    suffix = 1
    while destination.exists():
        destination = config.BACKUP_DIR / f"jobdeck_{key}_{stamp}-{suffix}.db"
        suffix += 1
    return destination


def _remove_partial(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _copy_and_verify(source: Path, destination: Path, expected: _DatabaseState) -> None:
    src = sqlite3.connect(source)
    try:
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    actual = _database_state(destination)
    if actual is None:
        raise sqlite3.DatabaseError("snapshot failed SQLite integrity validation")
    if actual != expected:
        raise sqlite3.DatabaseError("snapshot does not match the source database state")


def run_startup_backup() -> BackupResult:
    """Create and validate a snapshot, then rotate the lineage.

    The function never raises so UI callers can always report a useful result.
    Migration callers must check ``result.ok`` before changing the database.
    """
    destination: Path | None = None
    try:
        source = config.DB_PATH
        expected = _database_state(source)
        if expected is None:
            return BackupResult(
                error=f"Backup failed: {source} is not a valid JobDeck database."
            )

        key = _backup_key()
        best_name, best_count = _best_of(_scan(key))
        warning = _data_loss_warning(
            expected.application_count, best_name, best_count
        )

        config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        destination = _destination(key)
        try:
            _copy_and_verify(source, destination, expected)
        except Exception as exc:
            _remove_partial(destination)
            return BackupResult(error=f"Backup failed: {exc}")

        pairs = _scan(key)
        protected, _ = _best_of(pairs)
        excess = len(pairs) - BACKUP_KEEP
        rotation_errors: list[str] = []
        for name, _count in pairs:
            if excess <= 0:
                break
            if name == protected:
                continue
            try:
                (config.BACKUP_DIR / name).unlink()
            except OSError as exc:
                rotation_errors.append(str(exc))
                continue
            excess -= 1
        if rotation_errors:
            rotation_warning = "Backup created, but old snapshots could not be rotated."
            warning = f"{warning} {rotation_warning}".strip()
        return BackupResult(path=destination, warning=warning)
    except Exception as exc:
        if destination is not None:
            _remove_partial(destination)
        return BackupResult(error=f"Backup failed: {exc}")
