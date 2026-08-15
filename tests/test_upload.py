"""The one folder an employer's file picker opens in.

His report was "when the form asks for the file, it opens the previous
application's folder". These tests are about the two ways a staged file can
quietly be the WRONG file — a stale inode, and a leftover from an application
that is finished — because both look exactly like a correct one in a file
dialog.
"""

import os
import pathlib

import pytest

from jobdeck import config
from jobdeck.services import upload


def _pdf(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())
    return path


def test_a_built_mappe_is_offered_from_the_one_permanent_folder(data_dir):
    source = _pdf(data_dir / "output" / "job_7" / "Bewerbung_A_Firma.pdf", "eins")

    staged = upload.stage(source)

    assert staged.parent == pathlib.Path(config.UPLOAD_DIR)
    assert staged.name == "Bewerbung_A_Firma.pdf"   # the company is in the name
    assert staged.read_bytes() == b"eins"
    # a link, not a copy: no second Mappe on disk to drift from the archive
    assert staged.stat().st_ino == source.stat().st_ino


def test_two_applications_in_flight_keep_their_own_file(data_dir):
    """He opened six forms in thirteen minutes on the day this was measured.
    A folder holding one file would make five of six "Mappe bereit" lines
    false, and a file dialog shows nothing but the name."""
    first = upload.stage(
        _pdf(data_dir / "output" / "job_1" / "Bewerbung_A_Erste.pdf", "eins"))
    second = upload.stage(
        _pdf(data_dir / "output" / "job_2" / "Bewerbung_A_Zweite.pdf", "zwei"))

    assert first.exists() and second.exists()
    assert first.read_bytes() == b"eins"
    assert second.read_bytes() == b"zwei"


def test_a_rebuilt_mappe_replaces_the_staged_file_rather_than_refusing(data_dir):
    """`os.link` refuses an existing destination, and the destination existing
    is the NORMAL case — every rebuild re-stages over its own previous link."""
    source = _pdf(data_dir / "output" / "job_7" / "Bewerbung_A_Firma.pdf", "alt")
    upload.stage(source)

    source.write_bytes(b"neu")
    staged = upload.stage(source)

    assert staged.read_bytes() == b"neu"


def test_restaging_after_a_replace_follows_the_new_bytes(data_dir):
    """THE defect this module exists for. `pdf.install_pdf` and
    `pdf._write_deduplicated` both end in `replace()`, which gives the
    destination a NEW inode — the old link survives, opens fine, looks
    complete, and holds the previous application's letter.

    So the staging has to happen AFTER the build returns, and this proves the
    difference is observable: a link made before the replace is stale."""
    source = _pdf(data_dir / "output" / "job_7" / "Bewerbung_A_Firma.pdf", "alt")
    stale = upload.stage(source)

    # exactly what install_pdf does on the way out
    tmp = source.with_suffix(".pdf.part")
    tmp.write_bytes(b"neu")
    os.replace(tmp, source)

    assert stale.read_bytes() == b"alt", "the old link is stale, as expected"
    assert upload.stage(source).read_bytes() == b"neu"


def test_a_filesystem_without_hardlinks_still_gets_the_file(data_dir,
                                                            monkeypatch):
    """DATA_DIR is env-overridable, so it may be on another mount (EXDEV) or on
    exFAT and several network mounts (ENOTSUP). Without the fallback the whole
    feature is simply absent for anyone who moved their data directory."""
    source = _pdf(data_dir / "output" / "job_7" / "Bewerbung_A_Firma.pdf", "eins")
    monkeypatch.setattr(
        os, "link",
        lambda *a, **kw: (_ for _ in ()).throw(OSError(18, "Invalid cross-device link")))

    staged = upload.stage(source)

    assert staged.read_bytes() == b"eins"
    assert staged.stat().st_ino != source.stat().st_ino   # a copy this time


def test_clearing_takes_the_file_back_out(data_dir):
    source = _pdf(data_dir / "output" / "job_7" / "Bewerbung_A_Firma.pdf", "eins")
    staged = upload.stage(source)

    upload.clear(staged)

    assert not staged.exists()
    assert source.exists(), "the archive is not what gets removed"
    upload.clear(staged)          # idempotent: closing a loop twice is normal
    upload.clear("")              # and nothing staged is not an error


@pytest.mark.parametrize("victim", [
    "output/job_7/Bewerbung_A_Firma.pdf",       # the archive itself
    "profile.md",                               # anything else in the data dir
])
def test_clearing_refuses_a_path_outside_the_upload_folder(data_dir, victim):
    """The path comes out of a database column and this function deletes. A
    stale or hand-edited value must not be able to reach the archive — that is
    what `bewerbungen.dokument` points at, for applications already sent."""
    target = _pdf(data_dir / victim, "wichtig")

    upload.clear(target)

    assert target.exists()
