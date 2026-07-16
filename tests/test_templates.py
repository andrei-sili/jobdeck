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


def test_missing_body_token_is_an_error():
    with pytest.raises(templates.TemplateError):
        templates.render_letter("<p>{{BETREFF}}</p>", _values())
