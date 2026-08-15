"""The one place a form application is written into the ledger.

Two writers will eventually say "this went out" — his press, and an
Eingangsbestätigung read out of Gmail — so they call one function. The tests
below are mostly about what the two OLD writers disagreed on: 13 of his 35
Online-Portal ledger rows point at no document because one of them passed
`dokument` and the other did not.
"""

import pathlib

from jobdeck import db
from jobdeck.services import apply_record, upload


def _job(con, external_id="e1", company="Formular GmbH", **over):
    values = {"source": "arbeitsagentur", "external_id": external_id,
              "title": "Python Entwickler (m/w/d)", "company": company,
              "url": "https://www.arbeitsagentur.de/jobsuche/jobdetail/x"}
    values.update(over)
    job_id = db.insert_job_if_new(con, values)
    con.commit()
    return job_id


def _with_mappe(con, data_dir, job_id):
    """A posting the way press 1 leaves it: started, letter written, staged."""
    archive = (pathlib.Path(data_dir) / "output" / f"job_{job_id}"
               / "Bewerbung_A_Formular.pdf")
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(b"%PDF-1.4 mappe")
    db.mark_form_opened(con, job_id)
    db.upsert_draft(con, job_id, {"status": "ready",
                                  "anschreiben_body": "Sehr geehrte Damen,",
                                  "pdf_path": str(archive)})
    staged = upload.stage(archive)
    db.set_upload(con, job_id, str(staged), "vollständig")
    con.commit()
    return archive, staged


def test_recording_stores_the_document_that_was_actually_sent(con, data_dir):
    """The defect, named: `cockpit._record` called `apply_job` WITHOUT
    `dokument` while `jobs.confirm_applied` passed it, so on his real data 13
    of 35 Online-Portal rows point at nothing while the PDFs sit on disk.

    Nothing in the old suite asserted this — every db.apply_job test omitted
    the argument, so the omission was invisible."""
    job_id = _job(con)
    archive, _staged = _with_mappe(con, data_dir, job_id)

    result = apply_record.record_form_application(job_id)

    assert result["ok"]
    row = db.get_bewerbung(con, result["bewerbung_id"])
    assert row["dokument"] == str(archive)
    assert row["kanal"] == "Online-Portal"
    assert row["firma"] == "Formular GmbH"
    assert db.get_job(con, job_id)["status"] == "applied"
    assert db.get_job(con, job_id)["bewerbung_id"] == result["bewerbung_id"]


def test_the_ledger_points_at_the_archive_and_not_at_the_staged_copy(
        con, data_dir):
    """The staged file is a link that is re-made on every build and removed
    when the loop closes. Recording it would point the ledger at a path about
    to hold a Bewerbung for a different company."""
    job_id = _job(con)
    archive, staged = _with_mappe(con, data_dir, job_id)

    result = apply_record.record_form_application(job_id)

    assert db.get_bewerbung(con, result["bewerbung_id"])["dokument"] \
        == str(archive)
    assert str(staged) != str(archive)


def test_closing_the_loop_takes_the_file_out_of_the_upload_folder(
        con, data_dir):
    """Otherwise the next form's picker offers the Bewerbung he already sent
    to someone else — which is the defect the folder exists to end."""
    job_id = _job(con)
    archive, staged = _with_mappe(con, data_dir, job_id)
    assert staged.exists()

    apply_record.record_form_application(job_id)

    assert not staged.exists()
    assert archive.exists(), "the archive is not what gets removed"
    job = db.get_job(con, job_id)
    assert job["upload_path"] == "" and job["mappe_kind"] == ""


def test_a_second_application_at_one_company_is_refused_and_offers_no_undo(
        con, data_dir):
    """One application per company. `apply_job` marks the posting a duplicate
    and points it at the blocking application BEFORE returning None, so there
    is no earlier state an undo could restore — offering one would restore a
    state that never existed."""
    first = _job(con)
    _with_mappe(con, data_dir, first)
    apply_record.record_form_application(first)
    twin = _job(con, external_id="e2")

    result = apply_record.record_form_application(twin)

    assert not result["ok"]
    assert result["undo"] is False
    assert result["duplicate"]["firma"] == "Formular GmbH"
    assert db.get_job(con, twin)["status"] == "duplicate"


def test_the_undo_removes_every_write_the_recording_made(con, data_dir):
    """The 10-second "Rückgängig" replaces a confirmation dialog, and that
    trade is only honest if the undo is real. A half-undo leaves the company
    marked as applied-to and permanently spends its only application slot."""
    job_id = _job(con)
    _with_mappe(con, data_dir, job_id)
    history_before = con.execute("SELECT COUNT(*) FROM status_history").fetchone()[0]
    result = apply_record.record_form_application(job_id)
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 1
    assert con.execute(
        "SELECT COUNT(*) FROM status_history").fetchone()[0] > history_before

    apply_record.undo(job_id, result["bewerbung_id"], result["previous_status"])

    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 0
    assert con.execute(
        "SELECT COUNT(*) FROM status_history").fetchone()[0] == history_before
    job = db.get_job(con, job_id)
    assert job["status"] == "new"
    assert job["bewerbung_id"] is None
    # and the company is free again — the whole point of undoing
    assert db.find_duplicate_bewerbung(con, "Formular GmbH", "") is None


def test_the_undo_restores_what_the_posting_actually_was(con, data_dir):
    """Not a hardcoded 'new': a posting he had put away and then recorded from
    the strip must go back to being put away, not quietly revived into the
    working list."""
    job_id = _job(con)
    _with_mappe(con, data_dir, job_id)
    db.set_job_status(con, job_id, "skipped")
    con.commit()
    result = apply_record.record_form_application(job_id)

    apply_record.undo(job_id, result["bewerbung_id"], result["previous_status"])

    assert db.get_job(con, job_id)["status"] == "skipped"


def test_recording_a_posting_with_no_mappe_is_honest_rather_than_refused(
        con, data_dir):
    """If the documents could not be built he may still have applied by hand.
    The row is written with an empty `dokument` — saying so — rather than the
    application being lost because the app could not produce a PDF."""
    job_id = _job(con)
    db.mark_form_opened(con, job_id)
    con.commit()

    result = apply_record.record_form_application(job_id)

    assert result["ok"]
    assert db.get_bewerbung(con, result["bewerbung_id"])["dokument"] == ""


def test_a_vanished_posting_answers_instead_of_raising(con, data_dir):
    result = apply_record.record_form_application(999999)
    assert not result["ok"] and result["undo"] is False


def test_abandoning_a_start_takes_the_document_out_of_the_folder(con, data_dir):
    """A Mappe left in the upload folder for an application he abandoned is
    the next thing an employer's picker offers. The file has to go before the
    pointer is blanked — afterwards nothing in the app could find it."""
    job_id = _job(con)
    archive, staged = _with_mappe(con, data_dir, job_id)
    assert staged.exists()

    apply_record.abandon_form(job_id)

    assert not staged.exists()
    assert archive.exists(), "the archive is not what gets removed"
    job = db.get_job(con, job_id)
    assert job["form_opened_at"] == ""
    assert job["upload_path"] == "" and job["mappe_kind"] == ""


def test_the_undo_gives_the_documents_back_too(con, data_dir):
    """Recording performs FIVE writes, not four: it also clears the staged
    file and blanks the columns. An undo that reversed only `apply_job` handed
    him back an application whose strip entry read "Mappe NICHT fertig" while
    the complete Mappe sat untouched on disk, and whose "Ordner öffnen" had
    disappeared."""
    from jobdeck.ui.pages import jobs as jobs_page
    job_id = _job(con)
    archive, staged = _with_mappe(con, data_dir, job_id)
    result = apply_record.record_form_application(job_id)
    assert not staged.exists()

    apply_record.undo(job_id, result["bewerbung_id"], result["previous_status"])

    job = db.get_job(con, job_id)
    assert job["mappe_kind"] == "vollständig"
    assert job["upload_path"] != ""
    assert pathlib.Path(job["upload_path"]).exists()
    assert pathlib.Path(job["upload_path"]).read_bytes() == archive.read_bytes()
    assert jobs_page.mappe_line(dict(job)) == ("Mappe bereit", "")


def test_the_undo_survives_a_posting_that_was_refused_because_of_it(con, data_dir):
    """`jobs.duplicate_of` is a SECOND foreign key into the row being deleted,
    written by the very gate this application armed. Leaving it made the DELETE
    raise — inside a worker thread, so one log line, a bar that vanishes and a
    user who believes the undo happened — and those postings are not
    duplicates of an application that never existed."""
    first = _job(con)
    _with_mappe(con, data_dir, first)
    result = apply_record.record_form_application(first)
    twin = _job(con, external_id="e2")
    assert apply_record.record_form_application(twin)["ok"] is False
    assert db.get_job(con, twin)["duplicate_of"] == result["bewerbung_id"]

    apply_record.undo(first, result["bewerbung_id"], result["previous_status"])

    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 0
    twin_row = db.get_job(con, twin)
    assert twin_row["duplicate_of"] is None
    assert twin_row["status"] == "new", "left as a duplicate of nothing"
