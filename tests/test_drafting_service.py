

def test_the_daily_letter_quota_is_counted_and_then_refused(con, data_dir):
    """The cap is enforced inside the same transaction that commits the spend.

    Driven through `_claim` rather than through the screen: a screen that only
    greys out a button is one the keyboard, the batch and a second tab all
    walk past, and this is the layer all three share."""
    from jobdeck import db
    from jobdeck.services import drafting
    db.set_setting(con, "daily_draft_cap", "2")
    con.commit()
    jobs = [db.insert_job_if_new(con, {
        "source": "stub", "external_id": f"e{n}", "title": "Entwickler",
        "company": f"Firma {n}", "url": "https://firma.de/x"}) for n in range(3)]
    con.commit()

    assert drafting._claim(jobs[0]) == ""
    assert db.count_drafts_today(con) == 1
    assert drafting._claim(jobs[1]) == ""
    assert db.count_drafts_today(con) == 2

    refusal = drafting._claim(jobs[2])

    assert "2/2" in refusal and "Einstellungen" in refusal
    assert db.get_draft_by_job(con, jobs[2]) is None, "a claim was taken anyway"
    # …and raising it in Einstellungen lets the next one through
    db.set_setting(con, "daily_draft_cap", "3")
    con.commit()
    assert drafting._claim(jobs[2]) == ""


def test_a_failed_letter_still_counts_against_the_quota(con, data_dir):
    """It spent the tokens. The send cap counts test sends for the same
    reason — the money is gone whatever came back."""
    from jobdeck import db
    from jobdeck.services import drafting
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "e1", "title": "Entwickler",
        "company": "Firma", "url": "https://firma.de/x"})
    con.commit()

    assert drafting._claim(job_id) == ""
    db.upsert_draft(con, job_id, {"status": "failed", "error": "boom"})
    con.commit()

    assert db.count_drafts_today(con) == 1
