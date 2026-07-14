"""Automatic rotating database backups.

Protects against data loss (deleted, emptied, or corrupted database — e.g.
by a bad file operation): every app start takes a consistent snapshot into
the local backups directory. Design principles, hardened by regression
testing in the legacy tracker:

- snapshots use the sqlite3 backup API (consistent even mid-write), never
  a raw file copy, and are validated after creation;
- backups are keyed per database folder, so switching databases never
  raises false alarms;
- the loss-warning baseline is the BEST valid backup (not the newest), so
  the warning persists until resolved;
- rotation never deletes the best backup;
- snapshot filenames are collision-proof (microseconds + suffix);
- no backup failure may ever block application startup.
"""

import datetime
import hashlib
import os
import sqlite3
from pathlib import Path

from jobdeck import config

BACKUP_KEEP = 10  # snapshots kept per database folder


def _db_row_count(path: Path) -> int | None:
    """Application count in the database; None if missing or not a valid DB."""
    if not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect(path)
        try:
            return con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _backup_key() -> str:
    """Short identifier of the current DB folder (separate backup lineages)."""
    return hashlib.md5(str(config.DB_PATH.parent).encode("utf-8")).hexdigest()[:8]


def _list_backups(key: str) -> list[str]:
    """Backup filenames for the current lineage, oldest first. Never raises."""
    try:
        return sorted(
            f
            for f in os.listdir(config.BACKUP_DIR)
            if f.startswith(f"jobdeck_{key}_") and f.endswith(".db")
        )
    except OSError:
        return []


def run_startup_backup() -> str | None:
    """Snapshot the database and rotate old copies.

    Returns a human-readable warning when the database suddenly holds far
    fewer applications than the best backup (sign of accidental loss),
    otherwise None. Never raises.
    """
    warning = None
    try:
        db_path = config.DB_PATH
        key = _backup_key()
        current = _db_row_count(db_path)

        def scan() -> list[tuple[str, int]]:
            pairs = []
            for f in _list_backups(key):
                n = _db_row_count(config.BACKUP_DIR / f)
                if n is not None:
                    pairs.append((f, n))
            return pairs

        def best_of(pairs: list[tuple[str, int]]) -> tuple[str | None, int | None]:
            best_f, best_n = None, None
            for f, n in pairs:
                if best_n is None or n > best_n:
                    best_f, best_n = f, n
            return best_f, best_n

        best_file, best_count = best_of(scan())

        if best_count is not None and best_count >= 5 and (current or 0) < best_count / 2:
            warning = (
                f"The database holds far fewer applications than the best backup "
                f"({current or 0} instead of {best_count}). If data was lost, restore "
                f"from: {config.BACKUP_DIR / (best_file or '')} — close the app and "
                f"copy it to {db_path}. If you deleted entries on purpose, delete the "
                f"old backups in {config.BACKUP_DIR} to silence this warning."
            )

        if current:  # only snapshot a database that exists and has data
            os.makedirs(config.BACKUP_DIR, exist_ok=True)
            # Collision-proof name: a snapshot must never overwrite an
            # existing backup (it could be the best one).
            stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
            dest = config.BACKUP_DIR / f"jobdeck_{key}_{stamp}.db"
            n = 1
            while dest.exists():
                dest = config.BACKUP_DIR / f"jobdeck_{key}_{stamp}-{n}.db"
                n += 1
            try:
                src = sqlite3.connect(db_path)
                try:
                    dst = sqlite3.connect(dest)
                    try:
                        src.backup(dst)  # consistent snapshot, not a raw copy
                    finally:
                        dst.close()
                finally:
                    src.close()
                if _db_row_count(dest) is None:
                    raise sqlite3.Error("invalid snapshot")
            except Exception:
                # Failed snapshot (e.g. locked DB): never leave a partial
                # file behind posing as the newest backup.
                try:
                    if dest.exists():
                        os.remove(dest)
                except OSError:
                    pass
            else:
                # Rotate: drop the oldest beyond BACKUP_KEEP, but never
                # the best backup.
                pairs = scan()
                protect, _ = best_of(pairs)
                names = [f for f, _n in pairs]
                excess = len(names) - BACKUP_KEEP
                for f in names:
                    if excess <= 0:
                        break
                    if f == protect:
                        continue
                    os.remove(config.BACKUP_DIR / f)
                    excess -= 1
        return warning
    except Exception:
        # Any backup problem is less severe than an app that will not start.
        return warning
