"""The reply-ingestion repository layer (schema v12).

These pin the joins the match cascade rests on: which mail is already known,
which application a thread belongs to, which postings a receipt may match —
and that the shared signature SEES review actions that add no rows.
"""

import datetime

from jobdeck import db


def _job(con, external_id="j-1", company="Muster GmbH", **extra):
    values = {"source": "stub", "external_id": external_id, "company": company,
              "title": "Entwickler", "url": f"https://x.example/{external_id}"}
    values.update(extra)
    return db.insert_job_if_new(con, values)


def _bewerbung(con, firma="Muster GmbH", email="hr@muster.example",
               status="Gesendet"):
    return db.add_bewerbung(con, {"firma": firma, "email": email,
                                  "kanal": "E-Mail", "status": status})


def _inbound(con, message_id, *, thread="", bewerbung_id=None, job_id=None,
             needs_review=0, classification=""):
    return db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": message_id,
        "gmail_thread_id": thread, "bewerbung_id": bewerbung_id,
        "job_id": job_id, "needs_review": needs_review,
        "classification": classification,
    })


def test_known_gmail_ids_answers_only_what_the_log_holds(con):
    _inbound(con, "m-known")
    con.commit()
    assert db.known_gmail_ids(con, ["m-known", "m-new"]) == {"m-known"}
    assert db.known_gmail_ids(con, []) == set()


def test_thread_match_finds_the_application_a_send_created(con):
    bewerbung_id = _bewerbung(con)
    db.add_email_log(con, {"direction": "outbound", "gmail_message_id": "m-out",
                           "gmail_thread_id": "t-1",
                           "bewerbung_id": bewerbung_id})
    con.commit()
    assert db.find_bewerbung_by_thread(con, "t-1") == bewerbung_id
    assert db.find_bewerbung_by_thread(con, "t-unknown") is None
    assert db.find_bewerbung_by_thread(con, "") is None


def test_a_test_send_can_never_match_a_reply_to_an_application(con):
    """Rehearsal traffic carries no bewerbung_id — a reply landing in a test
    thread must not be tied to any application."""
    db.add_email_log(con, {"direction": "outbound_test",
                           "gmail_message_id": "m-test",
                           "gmail_thread_id": "t-test"})
    con.commit()
    assert db.find_bewerbung_by_thread(con, "t-test") is None


def test_thread_match_falls_back_to_the_draft_row(con):
    """A manually resolved send writes '' into email_log's thread id but the
    draft may still carry one from a later backfill — ask both."""
    bewerbung_id = _bewerbung(con)
    job_id = _job(con)
    con.execute(
        "INSERT INTO drafts (job_id, status, gmail_thread_id, bewerbung_id, "
        "created_at, updated_at) VALUES (?, 'sent', 't-d', ?, 't', 't')",
        (job_id, bewerbung_id))
    con.commit()
    assert db.find_bewerbung_by_thread(con, "t-d") == bewerbung_id


def test_reply_match_lists_only_applications_with_an_address(con):
    with_addr = _bewerbung(con, email="hr@muster.example")
    _bewerbung(con, firma="Portal AG", email="")
    con.commit()
    rows = db.bewerbungen_for_reply_match(con)
    assert [row["id"] for row in rows] == [with_addr]


def test_receipt_candidates_are_the_recent_strip_rows(con):
    now = datetime.datetime.now()
    recent = _job(con, "j-recent")
    db.mark_form_opened(con, recent)
    stale = _job(con, "j-stale")
    old = (now - datetime.timedelta(hours=100)).isoformat(timespec="seconds")
    con.execute("UPDATE jobs SET form_opened_at=? WHERE id=?", (old, stale))
    zombie = _job(con, "j-zombie")
    con.execute("UPDATE jobs SET form_opened_at='unbekannt' WHERE id=?",
                (zombie,))
    con.commit()

    cutoff = (now - datetime.timedelta(hours=72)).isoformat(timespec="seconds")
    assert [r["id"] for r in db.receipt_candidates(con, cutoff)] == [recent]


def test_a_recorded_application_stays_a_candidate_until_its_receipt_is_logged(con):
    """The healing arm: recording happened (his press, or a crash between the
    two receipt writes) but no inbound row exists yet — the next pass must
    still be able to attach the receipt instead of dropping it."""
    job_id = _job(con, "j-recorded")
    db.mark_form_opened(con, job_id)
    bewerbung_id = _bewerbung(con)
    db.set_job_status(con, job_id, "applied", bewerbung_id=bewerbung_id)
    con.commit()
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=72)
              ).isoformat(timespec="seconds")
    assert [r["id"] for r in db.receipt_candidates(con, cutoff)] == [job_id]

    _inbound(con, "m-receipt", bewerbung_id=bewerbung_id, job_id=job_id)
    con.commit()
    assert db.receipt_candidates(con, cutoff) == []


def test_review_piles_split_on_needs_review_and_direction(con):
    bewerbung_id = _bewerbung(con)
    pending = _inbound(con, "m-p", bewerbung_id=bewerbung_id, needs_review=1)
    settled = _inbound(con, "m-s", bewerbung_id=bewerbung_id,
                       classification="absage")
    db.add_email_log(con, {"direction": "outbound", "gmail_message_id": "m-o",
                           "needs_review": 1})  # never a review candidate
    con.commit()

    pending_rows = db.pending_review_replies(con)
    assert [row["id"] for row in pending_rows] == [pending]
    assert pending_rows[0]["bewerbung_firma"] == "Muster GmbH"
    settled_rows = db.list_inbound_replies(con)
    assert [row["id"] for row in settled_rows] == [settled]


def test_classify_link_and_reopen_roundtrip(con):
    bewerbung_id = _bewerbung(con)
    row_id = _inbound(con, "m-r", needs_review=1)
    con.commit()

    db.classify_reply_row(con, row_id, "einladung", "reply_manual", 0)
    db.link_reply_bewerbung(con, row_id, bewerbung_id)
    con.commit()
    row = db.get_email_log(con, row_id)
    assert (row["classification"], row["classified_by"], row["needs_review"],
            row["bewerbung_id"]) == ("einladung", "reply_manual", 0,
                                     bewerbung_id)

    db.link_reply_bewerbung(con, row_id, None)
    db.reopen_reply_review(con, row_id)
    con.commit()
    row = db.get_email_log(con, row_id)
    assert row["bewerbung_id"] is None
    assert row["needs_review"] == 1


def test_first_answer_dates_take_the_earliest_answered_transition(con):
    answered = _bewerbung(con)
    db.set_status(con, answered, "Antwort erhalten", source="user")
    db.set_status(con, answered, "Einladung", source="user")
    silent = _bewerbung(con, firma="Stille GmbH", email="hr@stille.example")
    con.commit()

    dates = db.first_answer_dates(con)
    assert silent not in dates
    history = db.list_status_history(con, answered)
    first_answer = min(row["created_at"] for row in history
                       if row["new_status"] == "Antwort erhalten")
    assert dates[answered] == first_answer


def test_the_shared_signature_sees_arrivals_and_review_actions(con):
    """A confirm flips needs_review without adding a row — the pages showing
    the pile must still notice, or the count on the rail goes stale."""
    before = db.data_signature(con)
    row_id = _inbound(con, "m-sig", needs_review=1)
    con.commit()
    after_arrival = db.data_signature(con)
    assert after_arrival != before

    db.classify_reply_row(con, row_id, "absage", "reply_manual", 0)
    con.commit()
    assert db.data_signature(con) != after_arrival
