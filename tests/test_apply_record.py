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
    """The current company-wide gate refuses and records a duplicate atomically.

    ADR 0002 narrows the target rule to posting and company-position identity,
    but this test preserves the behavior of the legacy gate until refactoring.
    """
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


# --------------------------------------------------------------------------
# The letter goes out with the application
# --------------------------------------------------------------------------
def test_recording_files_the_letter_that_went_out(con, data_dir):
    """The defect, measured on his data: recording left the letter at 'ready',
    so twelve of the seventeen letters waiting in his Postausgang were letters
    whose application had already gone out through a form that same afternoon
    — each one press away from a SECOND application at that company."""
    job_id = _job(con)
    _with_mappe(con, data_dir, job_id)

    result = apply_record.record_form_application(job_id)

    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == "filed"
    assert draft["bewerbung_id"] == result["bewerbung_id"]
    assert db.count_waiting_drafts(con) == 0, "it must leave the Postausgang"


async def test_a_filed_letter_may_not_be_rewritten(con, data_dir, monkeypatch):
    """Re-drafting one would rewrite the record of what an employer holds.

    Driven through `draft_for_job`, not asserted against the dict: the refusal
    lives in `_claim`, and a version of it that simply skipped 'filed' passed
    a test that only checked the key was present."""
    from jobdeck.services import drafting
    monkeypatch.setattr(drafting, "_ai_enabled", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    monkeypatch.setattr(drafting.profile, "load_profile", lambda: "Fakten")
    monkeypatch.setattr(drafting, "_applicant_name", lambda: "Andrei Sili")
    job_id = _job(con)
    _with_mappe(con, data_dir, job_id)
    apply_record.record_form_application(job_id)
    assert db.get_draft_by_job(con, job_id)["status"] == "filed"

    result = await drafting.draft_for_job(job_id)

    assert not result["ok"]
    assert "Bewerbung eingetragen" in result["error"]
    assert db.get_draft_by_job(con, job_id)["status"] == "filed"


def test_a_letter_approved_for_auto_send_is_filed_too(con, data_dir):
    """`FILEABLE` could be narrowed to ('ready',) with the suite green: the
    helper only ever builds a 'ready' draft, so the 'approved' arm was never
    executed."""
    job_id = _job(con, external_id="approved", company="Freigegeben GmbH")
    _with_mappe(con, data_dir, job_id)
    db.upsert_draft(con, job_id, {"status": "approved"})
    con.commit()

    apply_record.record_form_application(job_id)

    assert db.get_draft_by_job(con, job_id)["status"] == "filed"


def test_an_already_sent_letter_is_never_re_filed(con, data_dir):
    """`FILEABLE` could also be WIDENED to include 'sent' with the suite
    green. A sent draft is the record of an e-mail this app addressed;
    re-filing it would overwrite that record with a claim about a form."""
    job_id = _job(con, external_id="sent2", company="Schon Gesendet GmbH")
    _with_mappe(con, data_dir, job_id)
    draft_id = db.get_draft_by_job(con, job_id)["id"]
    db.record_send(con, draft_id, "gmail-9", "thread-9", None)
    con.commit()

    apply_record.record_form_application(job_id)

    assert db.get_draft_by_job(con, job_id)["status"] == "sent"


def test_only_a_finished_unsent_letter_is_filed(con, data_dir):
    """A letter still being written did not go into anyone's form, and one
    that failed was never written at all — filing either would claim an
    employer holds something that does not exist."""
    for status in ("generating", "failed", "discarded"):
        job_id = _job(con, external_id=f"e-{status}", company=f"{status} GmbH")
        db.mark_form_opened(con, job_id)
        db.upsert_draft(con, job_id, {"status": status})
        con.commit()

        result = apply_record.record_form_application(job_id)

        assert result["ok"]
        assert db.get_draft_by_job(con, job_id)["status"] == status


def test_the_undo_gives_the_letter_back(con, data_dir):
    """`drafts.bewerbung_id` is a THIRD foreign key into the row the undo
    deletes. Leaving it makes the DELETE raise — in a worker thread, so a log
    line and a bar that vanishes — and strands the letter in a state nothing
    can send and nothing can rewrite."""
    job_id = _job(con)
    _with_mappe(con, data_dir, job_id)
    result = apply_record.record_form_application(job_id)
    assert db.get_draft_by_job(con, job_id)["status"] == "filed"

    apply_record.undo(job_id, result["bewerbung_id"], result["previous_status"])

    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == "ready", "sendable again, and rewritable"
    assert draft["bewerbung_id"] is None
    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 0


def _preparable(con, external_id, draft_status):
    """A posting the daily batch would otherwise take, carrying one draft.

    Deliberately NOT built with `_with_mappe`: that helper stamps
    `form_opened_at`, and `jobs_to_prepare` excludes a started form on its own
    — so the assertion below would have held with any draft status at all.
    """
    job_id = _job(con, external_id=external_id, company=f"Firma {external_id}")
    con.execute("UPDATE jobs SET match_score=90, status='new', "
                "published_on=date('now') WHERE id=?", (job_id,))
    db.upsert_draft(con, job_id, {"status": draft_status})
    con.commit()
    return job_id


def test_a_filed_letter_never_earns_a_second_one(con, data_dir):
    """`jobs_to_prepare` skips a posting that already has a draft, so a state
    it does not know about would let the daily batch pay to write the letter a
    second time — for an application that is already out.

    The `discarded` posting is what makes this an assertion rather than a
    tautology: it proves the batch WOULD take this shape, so the filed one
    being absent is the status doing the work."""
    filed = _preparable(con, "filed", "filed")
    thrown_away = _preparable(con, "discarded", "discarded")

    taken = [row["id"] for row in db.jobs_to_prepare(
        con, limit=10, max_age_days=365, min_score=1)]

    assert thrown_away in taken, "the fixture is not excluded for other reasons"
    assert filed not in taken


def test_every_hand_recorded_channel_closes_its_letter(con, data_dir):
    """The first version of this rule required a complete Mappe on the FORM
    channel. It read well and left the headline defect half-fixed: a by-e-mail
    application recorded by hand went on offering its letter in the Postausgang
    for ever, at a company that already had an application. It also made the
    one-shot migration and this writer disagree about the same rows, which is
    what the security review refused to ship.

    'filed' therefore says only what this app can know — an application for
    this posting is in the ledger — and the SCREENS say how it went, from the
    ledger row's own channel and document."""
    by_mail = _job(con, external_id="mail", company="Per Mail GmbH")
    _with_mappe(con, data_dir, by_mail)
    # a form application whose Mappe never finished: the letter still closes,
    # because an application for the posting exists either way
    no_mappe = _job(con, external_id="broken", company="Halbe Mappe GmbH")
    db.mark_form_opened(con, no_mappe)
    db.upsert_draft(con, no_mappe, {"status": "ready",
                                    "anschreiben_body": "Sehr geehrte"})
    con.commit()

    assert apply_record.record_application(by_mail,
                                           apply_record.KANAL_EMAIL)["ok"]
    assert apply_record.record_form_application(no_mappe)["ok"]

    assert db.get_draft_by_job(con, by_mail)["status"] == "filed"
    assert db.get_draft_by_job(con, no_mappe)["status"] == "filed"
    assert db.count_waiting_drafts(con) == 0, "neither may go on waiting"


def test_deleting_the_application_hands_its_letter_back(con, data_dir):
    """Every route out of 'filed' keys on the application: `unfile_draft`
    matches on it, discard and restore refuse the status, and re-drafting is
    refused. Deleting the row and leaving the letter filed strands it for
    ever — unsendable, undiscardable, unrewritable — for an application the
    user has just said did not happen."""
    job_id = _job(con)
    _with_mappe(con, data_dir, job_id)
    result = apply_record.record_form_application(job_id)
    assert db.get_draft_by_job(con, job_id)["status"] == "filed"

    db.delete_bewerbung(con, result["bewerbung_id"])
    con.commit()

    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == "ready"
    assert draft["bewerbung_id"] is None


def test_a_sent_letter_survives_its_application_being_deleted(con, data_dir):
    """The asymmetry is deliberate and is about evidence: a sent letter has a
    Gmail message id, so it really left whatever the ledger says afterwards. A
    filed letter's only evidence IS the ledger row."""
    job_id = _job(con, external_id="sent", company="Gesendet GmbH")
    draft_id = db.upsert_draft(con, job_id, {"status": "ready"})
    bewerbung_id = db.apply_job(con, job_id, kanal="E-Mail")
    db.record_send(con, draft_id, "gmail-1", "thread-1", bewerbung_id)
    con.commit()

    db.delete_bewerbung(con, bewerbung_id)
    con.commit()

    assert db.get_draft_by_job(con, job_id)["status"] == "sent"


def test_the_undo_survives_an_e_mail_logged_against_the_application(con,
                                                                   data_dir):
    """`email_log.bewerbung_id` is a FOURTH foreign key into the row the undo
    deletes. `delete_bewerbung` clears it and this path did not, so the DELETE
    raised — in a worker thread, so a log line and a bar that vanishes."""
    job_id = _job(con)
    _with_mappe(con, data_dir, job_id)
    result = apply_record.record_form_application(job_id)
    db.add_email_log(con, {"direction": "outbound", "to_addr": "hr@x.example",
                           "bewerbung_id": result["bewerbung_id"]})
    con.commit()

    apply_record.undo(job_id, result["bewerbung_id"], result["previous_status"])

    assert con.execute("SELECT COUNT(*) FROM bewerbungen").fetchone()[0] == 0


def test_filing_clears_the_error_of_the_attempt_that_failed_before_it(con,
                                                                      data_dir):
    """Otherwise a letter filed after a failed attempt keeps its stale red
    line in the Postausgang, under a row that says it has gone out."""
    job_id = _job(con)
    _with_mappe(con, data_dir, job_id)
    db.upsert_draft(con, job_id, {"status": "ready", "error": "vorher kaputt"})
    con.commit()

    apply_record.record_form_application(job_id)

    assert db.get_draft_by_job(con, job_id)["error"] == ""


def test_the_undo_never_reaches_a_letter_that_was_e_mailed(con, data_dir):
    """`unfile_draft` matches on the status as well as the application. Without
    that guard the undo would turn a SENT draft — the record of a real Gmail
    message — back into a waiting letter."""
    job_id = _job(con, external_id="mailed", company="Gemailt GmbH")
    draft_id = db.upsert_draft(con, job_id, {"status": "ready"})
    bewerbung_id = db.apply_job(con, job_id, kanal="E-Mail")
    db.record_send(con, draft_id, "gmail-2", "thread-2", bewerbung_id)
    con.commit()

    db.unfile_draft(con, bewerbung_id)

    assert db.get_draft(con, draft_id)["status"] == "sent"
