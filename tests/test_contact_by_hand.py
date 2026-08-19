"""Contact details a person read off the posting itself.

The Arbeitsagentur puts an employer's address behind a CAPTCHA, which the app
must never solve — so for those postings the only reader who can ever see the
address is him, and until he can type it in they are e-mail applications the
app cannot make.
"""

import pytest

from jobdeck import db


def _posting(con, company="Beispiel Technik GmbH", channel="board_apply"):
    cur = con.execute(
        "INSERT INTO jobs (source, external_id, title, company, fetched_at,"
        " status, apply_channel) VALUES ('arbeitsagentur', ?, ?, ?, '2026-08-19',"
        " 'new', ?)",
        (f"ext-{company}", "Junior Python / Django Entwickler*in", company,
         channel),
    )
    return cur.lastrowid


def test_the_details_he_types_are_what_the_letter_will_read(data_dir, con):
    job_id = _posting(con)

    db.set_contact_details(con, job_id, {
        "contact_email": "info@example.de",
        "ansprechpartner": "Herr Dirk Beispiel",
        "contact_strasse": "Musterstr. 19",
        "contact_plz_ort": "12345 Beispielstadt",
        "contact_phone": "+49 30 1234567",
    })
    con.commit()

    row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["contact_email"] == "info@example.de"
    assert row["ansprechpartner"] == "Herr Dirk Beispiel"
    assert row["contact_strasse"] == "Musterstr. 19"
    assert row["contact_plz_ort"] == "12345 Beispielstadt"
    assert row["contact_phone"] == "+49 30 1234567"
    assert row["contact_source"] == "user"


def test_an_address_turns_a_form_posting_into_an_e_mail_one(data_dir, con):
    """The point of the whole thing: with somewhere to write to, the posting
    stops being a form job and the normal e-mail path opens."""
    job_id = _posting(con, channel="board_apply")

    db.set_contact_details(con, job_id, {"contact_email": "info@example.de"})
    con.commit()

    assert con.execute("SELECT apply_channel FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] == "direct_email"


def test_correcting_one_field_does_not_blank_the_others(data_dir, con):
    job_id = _posting(con)
    db.set_contact_details(con, job_id, {
        "contact_email": "falsch@example.de",
        "ansprechpartner": "Herr Dirk Beispiel",
        "contact_strasse": "Musterstr. 19",
    })
    con.commit()

    db.set_contact_details(con, job_id, {"contact_email": "richtig@example.de"})
    con.commit()

    row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["contact_email"] == "richtig@example.de"
    assert row["ansprechpartner"] == "Herr Dirk Beispiel"
    assert row["contact_strasse"] == "Musterstr. 19"


def test_nothing_is_written_when_nothing_was_typed(data_dir, con):
    job_id = _posting(con)
    db.set_contact_details(con, job_id, {})
    con.commit()

    row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["contact_source"] == ""
    assert row["apply_channel"] == "board_apply"


def test_the_company_name_cannot_be_edited_here(data_dir, con):
    """It is the dedupe key the send gate reads: editing it here would let one
    posting quietly become a different company's, and the gate that stops a
    second application to the same firm reads the new name."""
    assert "company" not in db.CONTACT_FIELDS
    assert "firma" not in db.CONTACT_FIELDS

    job_id = _posting(con, company="Beispiel Technik GmbH")
    db.set_contact_details(con, job_id, {"company": "Andere GmbH",
                                         "contact_email": "info@example.de"})
    con.commit()

    assert con.execute("SELECT company FROM jobs WHERE id=?",
                       (job_id,)).fetchone()[0] == "Beispiel Technik GmbH"


@pytest.mark.parametrize("field", db.CONTACT_FIELDS)
def test_surrounding_whitespace_is_stripped(data_dir, con, field):
    """These arrive by copy-paste from a posting, which brings its own spaces
    — and an address with a trailing space is a different recipient."""
    job_id = _posting(con)

    db.set_contact_details(con, job_id, {field: "  Wert  "})
    con.commit()

    assert con.execute(f"SELECT {field} FROM jobs WHERE id=?",  # noqa: S608
                       (job_id,)).fetchone()[0] == "Wert"
