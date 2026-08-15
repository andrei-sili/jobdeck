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
        # exactly what the drafting service stores: the e-mail variant
        "betreff": "Bewerbung als Python Entwickler (m/w/d), K-17 – Erika Muster",
        "email_body": "Mail.",
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


def _heavy_anlage(image_pdf, noisy_image, anlagen_dir: pathlib.Path,
                  name: str = "03_scan.pdf") -> pathlib.Path:
    """An Anlage whose scan is stored losslessly — the shape that actually
    costs megabytes in the real Mappe."""
    return image_pdf(anlagen_dir / name,
                     [{"image": noisy_image(1400, 1000), "lossless": True}])


async def test_mappe_compresses_to_the_channel_budget(
    con, data_dir, image_pdf, noisy_image
):
    job_id = _setup(con, data_dir)
    _heavy_anlage(image_pdf, noisy_image, data_dir / "anlagen")
    db.set_setting(con, "mappe_target_mb", "0.5")
    con.commit()

    result = await mappe.create_mappe(job_id)
    assert result["ok"], result["error"]
    assert result["size_bytes"] <= 0.5 * 1024 * 1024
    assert result["size_before_bytes"] > result["size_bytes"]
    assert "→" in result["compression"]
    assert result["warning"] == ""
    assert result["pages"] == 5


async def test_mappe_leaves_the_anlagen_folder_byte_identical(
    con, data_dir, image_pdf, noisy_image
):
    """The Anlagen are the user's curated originals. Compression happens on
    the merged copy in a temp dir; the source files are only ever read."""
    job_id = _setup(con, data_dir)
    anlagen_dir = data_dir / "anlagen"
    _heavy_anlage(image_pdf, noisy_image, anlagen_dir)
    db.set_setting(con, "mappe_target_mb", "0.5")
    con.commit()
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
              for p in sorted(anlagen_dir.iterdir())}

    assert (await mappe.create_mappe(job_id))["ok"]

    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
             for p in sorted(anlagen_dir.iterdir())}
    assert after == before


async def test_mappe_skips_compression_when_it_is_switched_off(
    con, data_dir, image_pdf, noisy_image
):
    job_id = _setup(con, data_dir)
    _heavy_anlage(image_pdf, noisy_image, data_dir / "anlagen")
    db.set_setting(con, "mappe_target_mb", "0.5")
    db.set_setting(con, "mappe_compress", "0")
    con.commit()

    result = await mappe.create_mappe(job_id)
    assert result["ok"], result["error"]
    assert result["compression"] == ""
    assert result["size_bytes"] == result["size_before_bytes"]
    assert result["size_bytes"] > 0.5 * 1024 * 1024
    assert "over the 0.5 MB target" in result["warning"]
    # the reason must be the one that applies: pointing at the quality floor
    # sends the user hunting for a limit instead of the switch they flipped
    assert "switched off in Settings" in result["warning"]
    assert "quality floor" not in result["warning"]


async def test_mappe_warns_when_the_quality_floor_blocks_the_target(
    con, data_dir, image_pdf, noisy_image
):
    """An unreachable target must not silently ship an illegible Zeugnis."""
    job_id = _setup(con, data_dir)
    _heavy_anlage(image_pdf, noisy_image, data_dir / "anlagen")
    db.set_setting(con, "mappe_target_mb", "0.001")
    con.commit()

    result = await mappe.create_mappe(job_id)
    assert result["ok"], result["error"]
    assert "quality floor" in result["warning"]
    assert result["size_bytes"] > 0.001 * 1024 * 1024


async def test_portal_channels_get_the_tighter_budget(con, data_dir):
    settings = {"target_mb": "3", "target_portal_mb": "2"}
    assert mappe.target_bytes(settings, "ats_form") == 2 * 1024 * 1024
    assert mappe.target_bytes(settings, "board_apply") == 2 * 1024 * 1024
    assert mappe.target_bytes(settings, "company_site") == 2 * 1024 * 1024
    # e-mail and an unresolved channel keep the roomier budget: degrading a
    # scan on a guess is silent, an oversized upload fails in front of the user
    assert mappe.target_bytes(settings, "direct_email") == 3 * 1024 * 1024
    assert mappe.target_bytes(settings, "unknown") == 3 * 1024 * 1024
    assert mappe.target_bytes(settings, "") == 3 * 1024 * 1024


async def test_unset_or_broken_size_budgets_fall_back_to_the_defaults(
    con, data_dir
):
    # "inf" and "1e400" parse as a float and then raise OverflowError on the
    # conversion to bytes — past the build's error handler, so the button
    # would just die. app_settings is a file the user is invited to edit.
    for raw in ("", "   ", "abc", "0", "-4", None, "inf", "-inf", "1e400", "nan"):
        settings = {"target_mb": raw, "target_portal_mb": raw}
        assert mappe.target_bytes(settings, "direct_email") == int(
            mappe.DEFAULT_TARGET_MB * 1024 * 1024
        )
        assert mappe.target_bytes(settings, "ats_form") == int(
            mappe.DEFAULT_PORTAL_TARGET_MB * 1024 * 1024
        )


async def test_mappe_renders_merges_and_persists(con, data_dir):
    job_id = _setup(con, data_dir)

    result = await mappe.create_mappe(job_id)
    assert result["ok"], result["error"]

    out = pathlib.Path(result["pdf_path"])
    assert out.name == "Bewerbung_Erika_Muster_Mueller_Soehne_GmbH.pdf"
    # per-job folder: clean recipient-facing filename, no cross-job overwrite
    assert out.parent == pathlib.Path(config.OUTPUT_DIR) / f"job_{job_id}"
    assert out.exists() and out.read_bytes()[:5] == b"%PDF-"
    # letter (1 page) + Anlagen (2 + 1) — merged in filename order
    assert result["pages"] == 4
    assert result["warning"] == ""
    assert result["anlagen"] == ["01_zeugnis.pdf", "02_zertifikat.pdf"]
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
    assert not result["ok"] and "Ort" in result["error"]

    db.set_setting(con, "applicant_ort", "Musterstadt")
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


async def test_two_postings_at_same_company_never_collide(con, data_dir):
    job_a = _setup(con, data_dir, with_anlagen=False)
    job_b = db.insert_job_if_new(con, {
        "source": "arbeitsagentur", "external_id": "REF-88",
        "title": "Java Entwickler", "company": "Müller & Söhne GmbH",
        "description": "desc",
    })
    db.upsert_draft(con, job_b, {
        "status": "ready", "anschreiben_body": "Anrede,\n\nText B.",
    })
    con.commit()

    result_a = await mappe.create_mappe(job_a)
    result_b = await mappe.create_mappe(job_b)
    assert result_a["ok"] and result_b["ok"]
    assert result_a["pdf_path"] != result_b["pdf_path"]  # same clean filename…
    assert (pathlib.Path(result_a["pdf_path"]).name
            == pathlib.Path(result_b["pdf_path"]).name)
    assert pathlib.Path(result_a["pdf_path"]).exists()  # …neither overwritten
    assert pathlib.Path(result_b["pdf_path"]).exists()


async def test_mappe_discards_result_when_draft_changed_mid_render(
    con, data_dir, monkeypatch
):
    """TOCTOU guard: a Re-draft during the Chrome render must not get the
    OLD letter's PDF stamped onto the NEW draft."""
    job_id = _setup(con, data_dir, with_anlagen=False)

    real_render = pdf.html_to_pdf

    def render_and_redraft(html_text, out_pdf):
        real_render(html_text, out_pdf)
        with db.db() as c:  # a concurrent Re-draft finishes mid-build
            db.upsert_draft(c, job_id, {"anschreiben_body": "NEUER TEXT"})

    monkeypatch.setattr(pdf, "html_to_pdf", render_and_redraft)
    result = await mappe.create_mappe(job_id)
    assert not result["ok"] and "changed while the Mappe was rendering" in result["error"]
    assert db.get_draft_by_job(con, job_id)["pdf_path"] == ""
    # the stale file was discarded, not left to be opened later
    out_dir = pathlib.Path(config.OUTPUT_DIR) / f"job_{job_id}"
    assert not any(out_dir.glob("*.pdf"))


async def test_a_finished_mappe_is_staged_where_the_file_picker_opens(
    con, data_dir
):
    """The complaint was "the form opens the previous application's folder".
    The archive is a new directory per application, and every file chooser
    reopens wherever it was last used — so it was one application behind by
    construction. One permanent folder ends that."""
    job_id = _setup(con, data_dir, with_anlagen=False)

    result = await mappe.create_mappe(job_id)

    assert result["ok"]
    job = db.get_job(con, job_id)
    staged = pathlib.Path(job["upload_path"])
    assert staged.parent == pathlib.Path(config.UPLOAD_DIR)
    assert staged.exists()
    assert staged.read_bytes() == pathlib.Path(result["pdf_path"]).read_bytes()
    # the build says what it produced; no screen has to guess from the disk
    assert job["mappe_kind"] == mappe.MAPPE_COMPLETE
    # and the archive is untouched — it is what bewerbungen.dokument points at
    assert pathlib.Path(result["pdf_path"]).exists()


async def test_a_rebuilt_mappe_stages_the_new_bytes_not_the_old_inode(
    con, data_dir
):
    """`compress_to_target` writes the output up to four times in one call and
    both installers end in `replace()`, which gives it a NEW inode. A link made
    at any earlier moment survives, opens fine, looks complete — and is the
    previous letter."""
    job_id = _setup(con, data_dir, with_anlagen=False)
    assert (await mappe.create_mappe(job_id))["ok"]

    db.upsert_draft(con, job_id, {"anschreiben_body": "Anrede,\n\nGANZ NEUER TEXT."})
    con.commit()
    result = await mappe.create_mappe(job_id)

    assert result["ok"]
    staged = pathlib.Path(db.get_job(con, job_id)["upload_path"])
    assert staged.read_bytes() == pathlib.Path(result["pdf_path"]).read_bytes()


async def test_a_mappe_discarded_mid_render_is_never_left_in_the_upload_folder(
    con, data_dir, monkeypatch
):
    """The worst outcome reachable in this slice. The TOCTOU branch unlinks a
    finished PDF when the draft moved while Chrome rendered — but a hardlink
    keeps the inode ALIVE. Staged too early, the upload folder would hold a
    complete, plausible Mappe carrying the OLD letter while the app reported
    the build failed, and that is the file the employer's picker offers."""
    job_id = _setup(con, data_dir, with_anlagen=False)
    real_render = pdf.html_to_pdf

    def render_and_redraft(html_text, out_pdf):
        real_render(html_text, out_pdf)
        with db.db() as c:
            db.upsert_draft(c, job_id, {"anschreiben_body": "NEUER TEXT"})

    monkeypatch.setattr(pdf, "html_to_pdf", render_and_redraft)
    result = await mappe.create_mappe(job_id)

    assert not result["ok"]
    assert not any(pathlib.Path(config.UPLOAD_DIR).glob("*.pdf")), \
        "a Mappe the app says failed is sitting in the upload folder"
    job = db.get_job(con, job_id)
    assert job["upload_path"] == ""
    assert job["mappe_kind"] == "", "nothing complete is staged, and it says so"


async def test_letter_values_use_nameless_betreff_and_german_date(
    con, data_dir, monkeypatch
):
    job_id = _setup(con, data_dir, with_anlagen=False)
    captured = {}
    real_render = mappe.templates.render_letter

    def capture(template_html, values):
        captured.update(values)
        return real_render(template_html, values)

    monkeypatch.setattr(mappe.templates, "render_letter", capture)
    assert (await mappe.create_mappe(job_id))["ok"]
    # letter Betreff: title + Refnr, WITHOUT the applicant name
    assert captured["betreff"] == "Bewerbung als Python Entwickler (m/w/d), K-17"
    assert "Erika" not in captured["betreff"]
    from jobdeck.dates import heute_de
    assert captured["datum"] == heute_de()
    assert captured["ort"] == "Musterstadt"


async def test_deckblatt_role_follows_the_same_subject_as_the_letter(
    con, data_dir, monkeypatch
):
    """The cover sheet must never advertise a different Stelle than the
    Betreff on the next page."""
    job_id = _setup(con, data_dir, with_anlagen=False)
    db.upsert_draft(con, job_id, {
        "betreff": "Bewerbung als Backend Entwickler (m/w/d), K-99 – Erika Muster",
    })
    con.commit()
    captured = {}
    real_render = mappe.templates.render_letter

    def capture(template_html, values):
        captured.update(values)
        return real_render(template_html, values)

    monkeypatch.setattr(mappe.templates, "render_letter", capture)
    assert (await mappe.create_mappe(job_id))["ok"]
    assert captured["deckblatt_rolle"] == "als Backend Entwickler (m/w/d), K-99"
    assert captured["betreff"] == "Bewerbung als Backend Entwickler (m/w/d), K-99"
    # one source, so the two can never name different roles
    assert captured["betreff"].endswith(captured["deckblatt_rolle"])


async def test_letter_betreff_follows_a_user_corrected_subject(
    con, data_dir, monkeypatch
):
    """The user fixes a wrong Refnr in the queue: the letter must cite the
    corrected one too — HR matches e-mail subject against the letter."""
    job_id = _setup(con, data_dir, with_anlagen=False)
    db.upsert_draft(con, job_id, {
        "betreff": "Bewerbung als Python Entwickler (m/w/d), K-99 – Erika Muster",
    })
    con.commit()
    captured = {}
    real_render = mappe.templates.render_letter

    def capture(template_html, values):
        captured.update(values)
        return real_render(template_html, values)

    monkeypatch.setattr(mappe.templates, "render_letter", capture)
    assert (await mappe.create_mappe(job_id))["ok"]
    assert captured["betreff"] == "Bewerbung als Python Entwickler (m/w/d), K-99"
    assert "K-17" not in captured["betreff"]  # not rebuilt from the posting


async def test_mappe_can_be_rebuilt_for_an_approved_draft(con, data_dir):
    """Editing an approved draft's letter clears the PDF — getting one back
    must not require un-approving first."""
    job_id = _setup(con, data_dir, with_anlagen=False)
    db.upsert_draft(con, job_id, {"status": "approved"})
    con.commit()

    result = await mappe.create_mappe(job_id)
    assert result["ok"], result["error"]
    assert db.get_draft_by_job(con, job_id)["pdf_path"] == result["pdf_path"]


async def test_mappe_refuses_an_empty_anschreiben(con, data_dir):
    job_id = _setup(con, data_dir, with_anlagen=False)
    db.upsert_draft(con, job_id, {"anschreiben_body": "   "})
    con.commit()

    result = await mappe.create_mappe(job_id)
    assert not result["ok"] and "no Anschreiben" in result["error"]


async def test_non_latin_applicant_name_keeps_filename_wellformed(con, data_dir):
    job_id = _setup(con, data_dir, with_anlagen=False,
                    applicant_name="Ольга Иванова")
    result = await mappe.create_mappe(job_id)
    assert result["ok"], result["error"]
    name = pathlib.Path(result["pdf_path"]).name
    assert name == "Bewerbung_Mueller_Soehne_GmbH.pdf"  # no double underscore


# ---------------------------------------------------------------------------
# Which address the letter's block carries — through the real builder, not
# through the pure function. The wiring is the part that can be reverted.
# ---------------------------------------------------------------------------
async def _captured_letter_values(con, data_dir, monkeypatch, job_id) -> dict:
    captured = {}
    real_render = mappe.templates.render_letter

    def capture(template_html, values):
        captured.update(values)
        return real_render(template_html, values)

    monkeypatch.setattr(mappe.templates, "render_letter", capture)
    assert (await mappe.create_mappe(job_id))["ok"]
    return captured


async def test_the_letter_uses_the_boards_work_address_when_none_is_stated(
        con, data_dir, monkeypatch):
    """725 of his 769 postings state no address in their prose, so the block
    was simply empty; the board states one for the work location."""
    job_id = _setup(con, data_dir, with_anlagen=False)
    con.execute("UPDATE jobs SET contact_strasse='', contact_plz_ort='', "
                "work_strasse='Musterstraße 26', "
                "work_plz_ort='54321 Beispielstadt' WHERE id=?", (job_id,))
    con.commit()

    captured = await _captured_letter_values(con, data_dir, monkeypatch, job_id)

    assert captured["strasse"] == "Musterstraße 26"
    assert captured["plz_ort"] == "54321 Beispielstadt"


async def test_a_staffing_firm_gets_no_address_rather_than_its_clients(
        con, data_dir, monkeypatch):
    """Under Arbeitnehmerüberlassung the work location belongs to a client, so
    the letter would be addressed to a company that is not the recipient."""
    job_id = _setup(con, data_dir, with_anlagen=False)
    con.execute("UPDATE jobs SET contact_strasse='', contact_plz_ort='', "
                "work_strasse='Musterstraße 26', "
                "work_plz_ort='54321 Beispielstadt', temp_agency=1 WHERE id=?",
                (job_id,))
    con.commit()

    captured = await _captured_letter_values(con, data_dir, monkeypatch, job_id)

    assert captured["strasse"] == "" and captured["plz_ort"] == ""


async def test_an_address_the_posting_states_still_wins_in_the_letter(
        con, data_dir, monkeypatch):
    job_id = _setup(con, data_dir, with_anlagen=False)
    con.execute("UPDATE jobs SET work_strasse='Musterstraße 26', "
                "work_plz_ort='54321 Beispielstadt' WHERE id=?", (job_id,))
    con.commit()

    captured = await _captured_letter_values(con, data_dir, monkeypatch, job_id)

    assert captured["strasse"] == "Weg 1"


# --------------------------------------------------------------------------
# One definition of what a Mappe is built from
# --------------------------------------------------------------------------
def test_the_signed_setting_keys_are_exactly_the_ones_read(con, data_dir):
    """BUILD_SETTING_KEYS is what a screen watches so a budget or a template
    change reaches it. It was a hand-copied second list beside the reader, and
    six of its seven keys could be deleted with the whole suite green — the
    next setting added would silently become a fact a screen states and no
    signature can see."""
    seen = []
    real_get = db.get_setting

    def spy(connection, key, default=""):
        seen.append(key)
        return real_get(connection, key, default)

    original = mappe.db.get_setting
    mappe.db.get_setting = spy
    try:
        settings = mappe.build_settings(con)
    finally:
        mappe.db.get_setting = original

    assert set(seen) == set(mappe.BUILD_SETTING_KEYS)
    assert len(mappe.BUILD_SETTING_KEYS) == len(set(mappe.BUILD_SETTING_KEYS))
    # and every field the build uses really comes from one of them
    assert set(settings) == {"applicant_name", "applicant_ort", "template_path",
                             "anlagen_dir", "compress", "target_mb",
                             "target_portal_mb"}


def test_the_cover_sheet_and_the_subject_name_the_same_role(con, data_dir):
    """Without a draft — always, for the specimen — the Deckblatt fell back to
    the RAW scraped title while the Betreff two pages later printed the cleaned
    one. He opens the specimen to see what an employer gets and finds board
    noise on page one."""
    job_id = db.insert_job_if_new(con, {
        "source": "arbeitsagentur", "external_id": "REF-NOISE",
        "title": "Ab sofort: Python Entwickler (m/w/d)Vollzeit",
        "company": "Neue Firma GmbH",
    })
    con.commit()
    job = db.get_job(con, job_id)

    values = mappe.letter_values(job, None, "Erika Muster", "Musterstadt")

    assert "Ab sofort" not in values["deckblatt_rolle"]
    assert "Vollzeit" not in values["deckblatt_rolle"]
    assert "Python Entwickler" in values["deckblatt_rolle"]
    # the two lines of the same document agree
    assert values["deckblatt_rolle"].removeprefix("als ") in values["betreff"]
