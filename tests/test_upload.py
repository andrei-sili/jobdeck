"""The one folder an employer's file picker opens in.

His report was "when the form asks for the file, it opens the previous
application's folder". These tests are about the two ways a staged file can
quietly be the WRONG file — a stale inode, and a leftover from an application
that is finished — because both look exactly like a correct one in a file
dialog.
"""

import os
import pathlib
import shutil

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


def test_failed_copy_never_replaces_a_valid_stage_with_partial_bytes(
    data_dir, monkeypatch
):
    source = _pdf(data_dir / "output" / "job_7" / "Bewerbung_A_Firma.pdf", "old")
    staged = upload.stage(source)
    replacement = source.with_suffix(".new")
    replacement.write_bytes(b"new complete bytes")
    os.replace(replacement, source)

    monkeypatch.setattr(
        os,
        "link",
        lambda *args: (_ for _ in ()).throw(OSError(18, "cross-device")),
    )

    def fail_copy(_source, target):
        pathlib.Path(target).write_bytes(b"partial")
        raise OSError("injected copy failure")

    monkeypatch.setattr(shutil, "copyfile", fail_copy)

    with pytest.raises(OSError, match="injected copy failure"):
        upload.stage(source, previous=str(staged))

    assert staged.read_bytes() == b"old"
    assert list(staged.parent.glob("*.part")) == []


def test_startup_recovery_removes_an_interrupted_partial_stage(con, data_dir):
    folder = pathlib.Path(data_dir, "Bewerbung-hochladen")
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / ".Bewerbung_A (restore-7-9).pdf.deadbeef.part"
    partial.write_bytes(b"candidate data")

    assert upload.recover_interrupted_undos(con) == 1
    assert not partial.exists()


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


def test_two_applications_that_produce_one_filename_never_share_a_file(data_dir):
    """`pdf.safe_filename` collapses every non-alphanumeric run and truncates,
    so "Müller & Co. KG" and "Mueller Co KG" produce ONE name — and two
    postings at one company produce one by construction (his corpus has an
    employer with 27 of them). Under the old per-job folders that could not
    happen. Sharing the file means one application's letter is uploaded to the
    other's employer, and closing one deletes the other's document."""
    a = _pdf(data_dir / "output" / "job_1" / "Bewerbung_X_Mueller_Co_KG.pdf", "A")
    b = _pdf(data_dir / "output" / "job_2" / "Bewerbung_X_Mueller_Co_KG.pdf", "B")

    sa = upload.stage(a)
    sb = upload.stage(b)

    assert sa != sb
    assert sa.read_bytes() == b"A"
    assert sb.read_bytes() == b"B"
    upload.clear(sa)
    assert sb.exists(), "closing one application deleted the other's document"


def test_a_rebuild_lands_on_its_own_file_rather_than_walking_along(data_dir):
    """Otherwise every rebuild of the second application would claim the next
    free name and the folder would fill with its own stale copies."""
    a = _pdf(data_dir / "output" / "job_1" / "Bewerbung_X_Firma.pdf", "A")
    b = _pdf(data_dir / "output" / "job_2" / "Bewerbung_X_Firma.pdf", "B")
    upload.stage(a)
    mine = upload.stage(b)

    b.write_bytes(b"B zwei")
    again = upload.stage(b, previous=str(mine))

    assert again == mine
    assert again.read_bytes() == b"B zwei"
    assert len(list(pathlib.Path(config.UPLOAD_DIR).glob("*.pdf"))) == 2


def test_the_sweep_removes_only_what_no_row_offers(con, data_dir):
    """The real folder held 22 Mappen from August applications recorded
    before staging was taken back on record. A file is kept exactly while a
    job's upload_path or a document row's staged_path names it."""
    from jobdeck import db
    folder = pathlib.Path(config.UPLOAD_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    kept_job = _pdf(folder / "Bewerbung_A_Firma.pdf", "job")
    kept_doc = _pdf(folder / "Lebenslauf_A_Firma.pdf", "doc")
    orphan = _pdf(folder / "Bewerbung_A_Alt.pdf", "alt")
    partial = _pdf(folder / ".Bewerbung_A_X.pdf.abc.part", "part")
    job_id = db.insert_job_if_new(con, {"source": "stub", "external_id": "s1",
                                        "title": "Dev", "company": "Firma"})
    db.mark_form_opened(con, job_id)
    db.set_upload(con, job_id, str(kept_job), "vollständig")
    db.set_documents(con, job_id, [{"kind": db.DOC_LEBENSLAUF, "path": "/x",
                                    "staged_path": str(kept_doc)}])
    con.commit()

    removed = upload.sweep_orphans(con)

    assert removed == ["Bewerbung_A_Alt.pdf"]
    assert kept_job.exists() and kept_doc.exists() and partial.exists()
    assert not orphan.exists()
    assert upload.sweep_orphans(con) == []

    # A staged file for a form never opened (an older flow staged ahead of
    # the press) is shown on no strip: swept. So is one whose job has long
    # since been applied to — 20 of the 21 leftovers in the real folder.
    con.execute("UPDATE jobs SET form_opened_at='' WHERE id=?", (job_id,))
    con.commit()
    kept_job.write_bytes(b"job")
    kept_doc.write_bytes(b"doc")
    assert sorted(upload.sweep_orphans(con)) == ["Bewerbung_A_Firma.pdf",
                                                 "Lebenslauf_A_Firma.pdf"]
    db.mark_form_opened(con, job_id)
    con.execute("UPDATE jobs SET status='applied' WHERE id=?", (job_id,))
    con.commit()
    kept_job.write_bytes(b"job")
    kept_doc.write_bytes(b"doc")
    assert sorted(upload.sweep_orphans(con)) == ["Bewerbung_A_Firma.pdf",
                                                 "Lebenslauf_A_Firma.pdf"]


def test_the_sweep_runs_at_startup(con, data_dir):
    from jobdeck import db
    folder = pathlib.Path(config.UPLOAD_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    orphan = _pdf(folder / "Bewerbung_A_Alt.pdf", "alt")
    db.connect().close()
    db.bootstrap()
    assert not orphan.exists()
