import pytest

from jobdeck import templates

# Generic fixture mirroring the real template's token layout — no personal data.
TEMPLATE = """\
<div class="letter">
  <div class="to">An<br>{{FIRMA}}<br>{{ANSPRECHPARTNER}}<br>{{STRASSE}}<br>{{PLZ_ORT}}</div>
  <div class="date">{{ORT}}, {{DATUM}}</div>
  <h2>{{BETREFF}}</h2>
  {{ANSCHREIBEN_BODY}}
  <p>Mit freundlichen Grüßen</p>
</div>
"""


def _values(**over):
    values = {
        "firma": "Neue Firma GmbH",
        "ansprechpartner": "Frau Weber",
        "strasse": "Weg 1",
        "plz_ort": "10115 Berlin",
        "ort": "Musterstadt",
        "datum": "16.07.2026",
        "betreff": "Bewerbung als Python Entwickler, K-17",
        "anschreiben_body": "Sehr geehrte Frau Weber,\n\nerster Absatz.\n\nzweiter Absatz.",
    }
    values.update(over)
    return values


def test_render_fills_all_tokens():
    out = templates.render_letter(TEMPLATE, _values())
    assert "{{" not in out  # no token survives
    assert "Neue Firma GmbH<br>Frau Weber<br>Weg 1<br>10115 Berlin" in out
    assert "Musterstadt, 16.07.2026" in out
    assert "<h2>Bewerbung als Python Entwickler, K-17</h2>" in out
    # body: Anrede + two paragraphs as styled <p> blocks
    assert out.count(f'<p style="{templates.BODY_P_STYLE}">') == 3
    assert "Sehr geehrte Frau Weber," in out


def test_empty_address_tokens_collapse_their_line_breaks():
    out = templates.render_letter(
        TEMPLATE, _values(ansprechpartner="", strasse=None)
    )
    # firma connects straight to plz_ort — no blank lines in the block
    assert "Neue Firma GmbH<br>10115 Berlin" in out


def test_values_are_html_escaped():
    out = templates.render_letter(TEMPLATE, _values(
        firma="Müller & Söhne <GmbH>",
        anschreiben_body='Absatz mit <script>alert("x")</script>',
    ))
    assert "Müller &amp; Söhne &lt;GmbH&gt;" in out
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_single_newlines_become_line_breaks_within_a_paragraph():
    out = templates.render_letter(
        TEMPLATE, _values(anschreiben_body="Zeile 1\nZeile 2")
    )
    assert "Zeile 1<br>Zeile 2" in out


def test_token_shaped_values_stay_literal():
    """A posting/LLM-derived value containing {{TOKEN}} text must not be
    re-substituted — single-pass rendering."""
    out = templates.render_letter(TEMPLATE, _values(firma="{{DATUM}} GmbH"))
    assert "{{DATUM}} GmbH" in out            # stays literal
    assert out.count("16.07.2026") == 1       # only the real date slot filled


def test_missing_body_token_is_an_error():
    with pytest.raises(templates.TemplateError):
        templates.render_letter("<p>{{BETREFF}}</p>", _values())


# ---------------------------------------------------------------------------
# Which address the letter's {{STRASSE}} / {{PLZ_ORT}} block carries
# ---------------------------------------------------------------------------
def _job_row(**over):
    row = {"contact_strasse": "", "contact_plz_ort": "", "work_strasse": "",
           "work_plz_ort": "", "temp_agency": 0}
    row.update(over)
    return row


def test_an_address_the_posting_states_wins():
    """It was extracted from the text the employer wrote — where a posting
    names a postal address, that is where it wants applications."""
    address = templates.letter_address(_job_row(
        contact_strasse="Musterweg 3", contact_plz_ort="12345 Musterstadt",
        work_strasse="Musterstraße 26", work_plz_ort="54321 Beispielstadt"))
    assert address == ("Musterweg 3", "12345 Musterstadt")


def test_the_board_address_stands_in_when_the_posting_states_none():
    """725 of his 769 postings state no address at all, so the block was
    simply empty — and the alternative was an LLM reading prose."""
    assert templates.letter_address(_job_row(
        work_strasse="Musterstraße 26", work_plz_ort="54321 Beispielstadt")) \
        == ("Musterstraße 26", "54321 Beispielstadt")


def test_a_staffing_firm_never_lends_its_client_s_address_to_the_letter():
    """Under Arbeitnehmerüberlassung the employer is the staffing firm and the
    work location is somebody else's site: the letter would be addressed to a
    company that is not the recipient."""
    assert templates.letter_address(_job_row(
        work_strasse="Musterstraße 26", work_plz_ort="54321 Beispielstadt",
        temp_agency=1)) == ("", "")


def test_a_half_stated_posting_address_is_not_completed_from_the_board():
    """Mixing two sources into one address block is how a real street lands
    under the wrong postcode."""
    assert templates.letter_address(_job_row(
        contact_plz_ort="12345 Musterstadt", work_strasse="Musterstraße 26",
        work_plz_ort="54321 Beispielstadt")) == ("", "12345 Musterstadt")
