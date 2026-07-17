"""Data-layer behaviour of the review queue page (no NiceGUI rendering)."""

from jobdeck import db
from jobdeck.ui.pages import queue


def _job_with_draft(con, status="ready", **over):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "j1", "title": "Python Dev",
        "company": "Firma GmbH", "contact_email": "hr@firma.de",
    })
    values = dict(
        status=status, recipient="hr@firma.de",
        betreff="Bewerbung als Python Dev, K-17 – Max Muster",
        email_body="Guten Tag,\n\nanbei meine Bewerbung.",
        anschreiben_body="Sehr geehrte Damen und Herren,\n\nAbsatz.",
        pdf_path="/tmp/mappe.pdf",
    )
    values.update(over)
    db.upsert_draft(con, job_id, values)
    con.commit()
    return job_id


def _edit(text="Guten Tag,\n\nneuer Text."):
    return {"recipient": "hr@firma.de",
            "betreff": "Bewerbung als Python Dev, K-17 – Max Muster",
            "email_body": text,
            "anschreiben_body": "Sehr geehrte Damen und Herren,\n\nAbsatz."}


def test_editing_an_approved_draft_returns_it_to_ready(con, data_dir):
    """Approval is content-specific: auto-send must not transmit text the
    user changed after approving it."""
    job_id = _job_with_draft(con, status="approved")

    draft, error = queue._save_draft(job_id, _edit(), clear_pdf=False)
    assert error == ""
    assert draft["status"] == "ready"
    assert draft["email_body"] == "Guten Tag,\n\nneuer Text."
    assert db.get_draft_by_job(con, job_id)["status"] == "ready"


def test_editing_a_ready_draft_keeps_it_ready(con, data_dir):
    job_id = _job_with_draft(con, status="ready")

    draft, error = queue._save_draft(job_id, _edit(), clear_pdf=False)
    assert error == "" and draft["status"] == "ready"


def test_a_stale_dialog_cannot_rewrite_a_sent_draft(con, data_dir):
    """The editor may sit open while auto-send transmits: saving then would
    falsify the record of what actually went out."""
    job_id = _job_with_draft(con, status="sent")

    draft, error = queue._save_draft(job_id, _edit(), clear_pdf=False)
    assert "no longer editable" in error
    assert draft["status"] == "sent"
    stored = db.get_draft_by_job(con, job_id)
    assert stored["email_body"] == "Guten Tag,\n\nanbei meine Bewerbung."


def test_a_stale_dialog_cannot_rewrite_a_sending_draft(con, data_dir):
    job_id = _job_with_draft(con, status="sending")

    _, error = queue._save_draft(job_id, _edit(), clear_pdf=False)
    assert "no longer editable" in error
    assert db.get_draft_by_job(con, job_id)["email_body"] \
        == "Guten Tag,\n\nanbei meine Bewerbung."


def test_clear_pdf_drops_the_stale_mappe(con, data_dir):
    job_id = _job_with_draft(con)

    draft, error = queue._save_draft(job_id, _edit(), clear_pdf=True)
    assert error == "" and draft["pdf_path"] == ""


def test_missing_draft_is_reported(con, data_dir):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "j9", "title": "x", "company": "y",
    })
    con.commit()

    draft, error = queue._save_draft(job_id, _edit(), clear_pdf=False)
    assert draft is None and "gone" in error


def test_failed_drafts_are_reachable_in_the_open_filter(con, data_dir):
    """Their only other surface is the Job inbox's Draft button, which
    disappears once the job leaves status 'new'."""
    job_id = _job_with_draft(con, status="failed")
    db.set_job_status(con, job_id, "portal")
    con.commit()

    rows = db.list_drafts_with_jobs(con, queue.FILTER_STATUSES["open"])
    assert [r["status"] for r in rows] == ["failed"]
