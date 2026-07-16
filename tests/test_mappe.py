import pathlib

from pypdf import PdfWriter

from jobdeck import config, db, pdf
from jobdeck.services import mappe

# Generic single-page template with the full token contract — no personal data.
TEMPLATE = """\
<div>
  <div>{{FIRMA}}<br>{{ANSPRECHPARTNER}}<br>{{STRASSE}}<br>{{PLZ_ORT}}</div>
  <div>{{ORT}}, {{DATUM}}</div>
  <h2>{{BETREFF}}</h2>
  {{ANSCHREIBEN_BODY}}
  <p>Mit freundlichen Grüßen</p>
</div>
"""


def _blank_pdf(path: pathlib.Path, pages: int = 1) -> pathlib.Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    with path.open("wb") as fh:
        writer.write(fh)
    return path


def _setup(con, data_dir, with_anlagen=True, **setting_overrides):
    job_id = db.insert_job_if_new(con, {
        "source": "arbeitsagentur", "external_id": "REF-77",
        "title": "Python Entwickler (m/w/d)", "company": "Müller & Söhne GmbH",
        "description": "desc", "contact_email": "jobs@mueller.de",
    })
    db.set_job_contacts(con, job_id, {
        "ansprechpartner": "Frau Weber", "contact_strasse": "Weg 1",
        "contact_plz_ort": "52062 Aachen", "refnr": "K-17",
    })
    db.upsert_draft(con, job_id, {
        "status": "ready", "recipient": "jobs@mueller.de",
        "betreff": "egal", "email_body": "Mail.",
        "anschreiben_body": "Sehr geehrte Frau Weber,\n\nAbsatz eins.\n\nAbsatz zwei.",
    })
    template_file = data_dir / "template.html"
    template_file.write_text(TEMPLATE, encoding="utf-8")
    settings = {
        "applicant_name": "Erika Muster",
        "applicant_ort": "Musterstadt",
        "template_path": str(template_file),
    }
    if with_anlagen:
        anlagen = data_dir / "anlagen"
        anlagen.mkdir()
        _blank_pdf(anlagen / "01_zeugnis.pdf", pages=2)
        _blank_pdf(anlagen / "02_zertifikat.pdf", pages=1)
        settings["anlagen_dir"] = str(anlagen)
    settings.update(setting_overrides)
    for key, value in settings.items():
        db.set_setting(con, key, value)
    con.commit()
    return job_id


async def test_mappe_renders_merges_and_persists(con, data_dir):
    job_id = _setup(con, data_dir)

    result = await mappe.create_mappe(job_id)
    assert result["ok"], result["error"]

    out = pathlib.Path(result["pdf_path"])
    assert out.name == "Bewerbung_Erika_Muster_Mueller_Soehne_GmbH.pdf"
    assert out.parent == pathlib.Path(config.OUTPUT_DIR)
    assert out.exists() and out.read_bytes()[:5] == b"%PDF-"
    # letter (1 page) + Anlagen (2 + 1) — merged in filename order
    assert result["pages"] == 4
    assert result["warning"] == ""
    assert db.get_draft_by_job(con, job_id)["pdf_path"] == str(out)
    # the draft's other fields survive the pdf_path update
    assert db.get_draft_by_job(con, job_id)["anschreiben_body"].startswith("Sehr")


async def test_mappe_without_anlagen_is_just_the_letter(con, data_dir):
    job_id = _setup(con, data_dir, with_anlagen=False)
    result = await mappe.create_mappe(job_id)
    assert result["ok"], result["error"]
    assert result["pages"] == 1


async def test_mappe_gates_fail_with_readable_errors(con, data_dir):
    # no draft at all
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "x", "title": "T", "company": "C",
    })
    con.commit()
    result = await mappe.create_mappe(job_id)
    assert not result["ok"] and "draft the application first" in result["error"]

    # draft exists but not ready
    db.upsert_draft(con, job_id, {"status": "failed", "error": "boom"})
    con.commit()
    assert not (await mappe.create_mappe(job_id))["ok"]

    # ready draft but missing settings
    db.upsert_draft(con, job_id, {"status": "ready", "anschreiben_body": "A."})
    con.commit()
    result = await mappe.create_mappe(job_id)
    assert not result["ok"] and "applicant name" in result["error"]

    db.set_setting(con, "applicant_name", "Erika Muster")
    con.commit()
    result = await mappe.create_mappe(job_id)
    assert not result["ok"] and "template path" in result["error"]

    db.set_setting(con, "template_path", str(data_dir / "missing.html"))
    con.commit()
    result = await mappe.create_mappe(job_id)
    assert not result["ok"] and "not found" in result["error"]

    assert not (await mappe.create_mappe(99999))["ok"]


async def test_mappe_untokenized_template_fails_loudly(con, data_dir):
    job_id = _setup(con, data_dir, with_anlagen=False)
    (data_dir / "template.html").write_text("<p>{{BETREFF}}</p>", encoding="utf-8")
    result = await mappe.create_mappe(job_id)
    assert not result["ok"] and "ANSCHREIBEN_BODY" in result["error"]


async def test_mappe_size_warning(con, data_dir, monkeypatch):
    job_id = _setup(con, data_dir)
    monkeypatch.setattr(pdf, "MAX_MAPPE_BYTES", 100)  # force the guard
    result = await mappe.create_mappe(job_id)
    assert result["ok"]
    assert "5 MB" in result["warning"]
