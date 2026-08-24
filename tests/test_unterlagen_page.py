"""The Unterlagen screen, rendered for real.

What it must state is testable only by drawing it: the stack has to name the
pages an employer turns, the letter head has to name the fields an application
would leave blank, and the register has to be honest about the one thing it
does not do yet.
"""

import asyncio
import io
import pathlib
import sys

import pytest
from nicegui import ui
from nicegui.testing import User
from pypdf import PdfWriter

from jobdeck import db
from jobdeck.services import anlagen as anlagen_service
from jobdeck.ui.pages import unterlagen as unterlagen_page

pytest_plugins = ["nicegui.testing.user_plugin"]

pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_main.py")


@pytest.fixture(autouse=True)
def _keep_the_package_importable():
    """See test_draft_visibility_pages.py: NiceGUI's teardown pops the page
    module AND its parents out of sys.modules."""
    saved = {name: mod for name, mod in sys.modules.items()
             if name == "jobdeck" or name.startswith("jobdeck.")}
    yield
    sys.modules.update(saved)


def _marked(user: User, marker: str) -> list:
    """Elements carrying `marker`, and an empty list when there are none.

    `user.find(marker=…)` RAISES when nothing matches, so it can assert that a
    control exists but never that it is absent — and absence is half of what
    the row controls have to promise."""
    with user.client:
        return [el for el in user.client.elements.values()
                if marker in getattr(el, "_markers", [])]


def _pdf_bytes(pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _blank_pdf(path: pathlib.Path, pages: int = 1) -> pathlib.Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    with path.open("wb") as fh:
        writer.write(fh)
    return path


def _posting(con, company="Beispiel GmbH", **contacts):
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "e1", "title": "Python Entwickler",
        "company": company, "url": "https://beispiel.example/1",
    })
    if contacts:
        db.set_job_contacts(con, job_id, contacts)
    con.commit()
    return job_id


def _anlagen(con, data_dir) -> pathlib.Path:
    folder = data_dir / "anlagen"
    folder.mkdir(exist_ok=True)
    _blank_pdf(folder / "01_Zeugnis.pdf", pages=2)
    _blank_pdf(folder / "02_Zertifikat.pdf", pages=1)
    db.set_setting(con, "anlagen_dir", str(folder))
    con.commit()
    return folder


def _letter(con, job_id, body):
    db.upsert_draft(con, job_id, {"status": "ready", "anschreiben_body": body})
    con.commit()


# --------------------------------------------------------------------------
# The stack
# --------------------------------------------------------------------------
async def test_the_stack_names_every_part_with_its_pages_and_weight(
        user: User, con, data_dir):
    _posting(con)
    _anlagen(con, data_dir)

    await user.open("/unterlagen")

    await user.should_see("Die Mappe, Seite für Seite")
    await user.should_see("01_Zeugnis")
    await user.should_see("02_Zertifikat")
    await user.should_see("2 Seiten")
    # the weight column is the point of "a measured stack" — it is what tells
    # him which Anlage to shrink under the 5 MB ceiling
    await user.should_see("KB")
    # nothing has been built yet, so the total is not claimed
    await user.should_see("Noch nicht gebaut")


async def test_before_a_build_no_page_number_is_claimed(user: User, con,
                                                        data_dir):
    """The running app printed "1–2" beside the Zeugnis before anything was
    built — three pages early, because the letter had not been measured yet."""
    _posting(con)
    _anlagen(con, data_dir)

    await user.open("/unterlagen")

    await user.should_see("Noch nicht gebaut")
    await user.should_not_see("1–2")
    await user.should_not_see("4–5")


async def test_a_torn_anlage_is_named_on_the_screen(user: User, con, data_dir):
    """The alternative to finding it here is finding it at send time."""
    _posting(con)
    folder = _anlagen(con, data_dir)
    (folder / "03_Kaputt.pdf").write_bytes(b"not a pdf at all")

    await user.open("/unterlagen")

    await user.should_see("03_Kaputt")
    await user.should_see("nicht lesbar")


async def test_a_missing_anlagen_folder_is_stated_not_swallowed(
        user: User, con, data_dir):
    """In German, naming the path, with the repair beside it.

    It used to surface `pdf.PdfError`'s own English sentence — "Anlagen folder
    does not exist: /…" — in the middle of a German screen whose credibility
    rests on its German, and with nothing to press."""
    _posting(con)
    db.set_setting(con, "anlagen_dir", str(data_dir / "gibt-es-nicht"))
    con.commit()

    await user.open("/unterlagen")

    await user.should_see("Diesen Ordner gibt es nicht")
    await user.should_see(str(data_dir / "gibt-es-nicht"))
    await user.should_see("Ordner anlegen und verwenden")
    await user.should_not_see("does not exist")


async def test_no_folder_at_all_reads_differently_from_an_empty_one(
        user: User, con, data_dir):
    """Two states that used to look identical — and both looked like a Mappe
    that was simply short one certificate."""
    _posting(con)
    con.commit()

    await user.open("/unterlagen")

    await user.should_see("Noch kein Ordner für deine Anlagen")
    await user.should_see("Ordner anlegen und verwenden")


async def test_an_empty_folder_says_the_mappe_would_be_the_letter_alone(
        user: User, con, data_dir):
    """A folder with nothing in it drew a stack of one part and no complaint,
    which is exactly what a correct Mappe looks like."""
    empty = data_dir / "Anlagen"
    empty.mkdir()
    db.set_setting(con, "anlagen_dir", str(empty))
    _posting(con)
    con.commit()

    await user.open("/unterlagen")

    await user.should_see("Der Ordner ist leer")
    await user.should_see("ohne ein einziges Zeugnis")


async def test_the_order_of_the_stack_says_where_it_comes_from(
        user: User, con, data_dir):
    """The filenames are the single source of that order. A screen that let it
    be set somewhere else too would have two, and they would drift."""
    _posting(con)
    _anlagen(con, data_dir)

    await user.open("/unterlagen")

    await user.should_see("Reihenfolge ist die Reihenfolge der Dateinamen")
    # The arrows are a second way to set it, so the screen has to say they are
    # the SAME mechanism — otherwise there are two orders and they drift.
    await user.should_see("Die Pfeile benennen die Dateien um")


# --------------------------------------------------------------------------
# The letter head
# --------------------------------------------------------------------------
async def test_the_preview_is_filled_with_a_real_posting(user: User, con,
                                                         data_dir):
    _posting(con, company="Beispiel GmbH", ansprechpartner="Frau Weber",
             contact_strasse="Weg 1", contact_plz_ort="10115 Berlin")
    db.set_setting(con, "applicant_ort", "Musterstadt")
    con.commit()

    await user.open("/unterlagen")

    await user.should_see("Vorschau mit echten Daten")
    await user.should_see("Gefüllt mit: Beispiel GmbH")
    await user.should_see("Frau Weber")
    await user.should_see("10115 Berlin")
    # the Betreff is the most rule-bound line in the German norm, and checking
    # it before a PDF exists is half of why this panel is here
    await user.should_see("Bewerbung als Python Entwickler")
    await user.should_see("Musterstadt,")
    await user.should_see("Kein Feld bleibt leer")


async def test_a_field_the_posting_cannot_fill_is_named_with_its_reason(
        user: User, con, data_dir):
    """"Sehr geehrte Damen und Herren" is what the letter will say — the
    screen names the consequence, not just the gap."""
    _posting(con, company="Beispiel GmbH")
    db.set_setting(con, "applicant_ort", "Musterstadt")
    con.commit()

    await user.open("/unterlagen")

    await user.should_see("Ansprechpartner")
    await user.should_see("Sehr geehrte Damen und Herren")


async def test_with_no_open_posting_the_preview_refuses_to_invent_one(
        user: User, con, data_dir):
    """Most of his postings state no postal address at all, so a preview
    filled with plausible values would report a completeness he does not have."""
    await user.open("/unterlagen")

    await user.should_see("Keine offene Anzeige")
    await user.should_see("nie mit erfundenen Werten")


# --------------------------------------------------------------------------
# The register
# --------------------------------------------------------------------------
async def test_the_register_counts_the_letters_that_claimed_each_permission(
        user: User, con, data_dir):
    job_id = _posting(con)
    _letter(con, job_id, "… FastAPI und PostgreSQL im Abschlussprojekt …")
    db.add_claim(con, {"fact": "FastAPI, PostgreSQL", "binding": "IHK-Projekt",
                       "terms": "FastAPI", "state": "confirmed"})
    db.add_claim(con, {"fact": "Java & Spring Boot", "binding": "Eigenprojekt",
                       "terms": "Spring Boot", "state": "confirmed"})
    con.commit()

    await user.open("/unterlagen")

    await user.should_see("Was ein Brief behaupten darf")
    await user.should_see("IHK-Projekt")
    await user.should_see("in 1 Brief")
    # the permission no letter has ever used is the number worth having
    await user.should_see("noch nie")


async def test_a_permission_with_no_terms_says_it_cannot_be_counted(
        user: User, con, data_dir):
    """Never-looked-for and looked-for-and-never-found are different answers;
    showing "noch nie" for the first invites deleting a live permission."""
    job_id = _posting(con)
    _letter(con, job_id, "… Django im Praktikum …")
    db.add_claim(con, {"fact": "Django & DRF", "binding": "Praktikum",
                       "state": "confirmed"})
    con.commit()

    await user.open("/unterlagen")

    await user.should_see("nicht zählbar")


async def test_the_screen_names_what_the_register_does_not_yet_stand_for(
        user: User, con, data_dir):
    """The measurement that decides when the register may replace profile.md
    as the factual boundary. A section nothing confirmed stands for is a part
    of himself a letter drawing only on confirmed facts would drop."""
    from jobdeck import config
    _posting(con)
    config.PROFILE_PATH.write_text(
        "## Technische Kenntnisse\nPython\n## Zertifikate\nEins\n",
        encoding="utf-8")
    db.add_claim(con, {"fact": "Python", "binding": "Eigenprojekt",
                       "terms": "Python", "state": "confirmed",
                       "source": "profile_md",
                       "source_ref": "Technische Kenntnisse"})
    con.commit()

    await user.open("/unterlagen")

    await user.should_see("1 von 2 Abschnitten")
    await user.should_see("Noch nichts bestätigt aus: Zertifikate")


async def test_a_proposal_does_not_stand_for_a_section(user: User, con,
                                                       data_dir):
    """Otherwise the register looks ready the moment it is read — the one
    moment nobody has checked it."""
    from jobdeck import config
    _posting(con)
    config.PROFILE_PATH.write_text("## Zertifikate\nEins\n", encoding="utf-8")
    db.add_claim(con, {"fact": "IHK", "binding": "", "source": "profile_md",
                       "source_ref": "Zertifikate"})
    con.commit()

    await user.open("/unterlagen")

    # Inflected: one section is "Abschnitt", and zero of them "sind".
    await user.should_see("0 von 1 Abschnitt ")
    await user.should_see("Noch nichts bestätigt aus: Zertifikate")


async def test_the_register_says_it_does_not_yet_constrain_the_prompt(
        user: User, con, data_dir):
    """The mirror-first decision, pinned. The drafting prompt still reads
    profile.md; a screen implying otherwise would be believed at exactly the
    moment a letter goes out."""
    _posting(con)
    await user.open("/unterlagen")

    # NOT just "profile.md" — that string is also the button label two rows
    # up, so the assertion passed with the whole paragraph deleted.
    await user.should_see("Heute zählt dieses Register mit")
    await user.should_see("Was die KI behaupten DARF")


async def test_a_permission_added_through_the_dialog_is_stored(
        user: User, con, data_dir):
    _posting(con)

    await user.open("/unterlagen")
    user.find("Erlaubnis hinzufügen").click()
    await asyncio.sleep(0.2)

    inputs = [e for e in user.client.elements.values()
              if isinstance(e, ui.input)]
    labels = [e.props.get("label", "") for e in inputs]
    inputs[next(i for i, t in enumerate(labels) if t.startswith("Was"))] \
        .set_value("Java & Spring Boot")
    inputs[next(i for i, t in enumerate(labels) if t.startswith("Wobei"))] \
        .set_value("Eigenprojekt")
    inputs[next(i for i, t in enumerate(labels) if t.startswith("Wörter"))] \
        .set_value("Spring Boot")
    await asyncio.sleep(0.1)
    user.find("Speichern").click()
    await asyncio.sleep(0.4)

    rows = db.list_claims(con)
    assert [(r["fact"], r["binding"], r["terms"]) for r in rows] == [
        ("Java & Spring Boot", "Eigenprojekt", "Spring Boot")]


async def test_reading_the_profile_is_refused_while_the_ai_switch_is_off(
        user: User, con, data_dir):
    """The button is wired to the real service, and the master switch's
    promise reaches the screen rather than only the log. `ai_enabled` is off
    by default, so this is also the state he will meet it in."""
    _posting(con)

    await user.open("/unterlagen")
    user.find(marker="propose-claims").click()
    await asyncio.sleep(0.2)

    # it costs money, so it asks first — and says so before spending
    await user.should_see("Ein Aufruf über deine profile.md")
    user.find(marker="confirm-propose").click()
    await asyncio.sleep(0.4)

    await user.should_see("ausgeschaltet")
    assert db.list_claims(con) == []


async def test_deleting_a_permission_asks_first(user: User, con, data_dir):
    _posting(con)
    # Confirmed: deleting is what he does to a permission he has vouched for.
    # A proposal is refused instead, which keeps the row.
    claim_id = db.add_claim(con, {"fact": "Java", "binding": "Eigenprojekt",
                                  "state": "confirmed"})
    con.commit()

    await user.open("/unterlagen")
    user.find(marker="delete-claim").click()
    await asyncio.sleep(0.2)

    await user.should_see("löschen?")
    assert db.list_claims(con), "deleted before asking"

    user.find(marker="confirm-delete-claim").click()
    await asyncio.sleep(0.3)
    assert [r["id"] for r in db.list_claims(con)] == [], claim_id


# --------------------------------------------------------------------------
# The search profile, and the address it moved from
# --------------------------------------------------------------------------
async def test_the_search_profile_reads_as_a_summary(user: User, con, data_dir):
    db.add_profile(con, {"name": "Python", "keywords": "Python Entwickler",
                         "hard_tags": "Keine Ausbildungsstelle"})
    db.set_setting(con, "stale_age_days", "30")
    con.commit()

    await user.open("/unterlagen")

    await user.should_see("Suchprofil")
    await user.should_see("Python Entwickler")
    await user.should_see("ganz Deutschland")
    await user.should_see("Keine Ausbildungsstelle")
    await user.should_see("30")


async def test_the_old_profiles_address_still_lands(user: User, con, data_dir):
    """A bookmark or an old link must not answer with nothing."""
    db.add_profile(con, {"name": "Python", "keywords": "Python Entwickler"})
    con.commit()

    await user.open("/profiles")

    # the screen itself, not the tab title — a redirect that rendered nothing
    # would still have set the title
    await user.should_see("Die Mappe, Seite für Seite")
    await user.should_see("Suchprofil")


# --------------------------------------------------------------------------
# The half of the Mappe panel that only exists after a build
# --------------------------------------------------------------------------
def _built_specimen(con, data_dir, *, letter_pages=3, total=6, before=0,
                    lossless=False):
    """The artifact and the facts a real build leaves behind.

    Every page test runs with a fresh OUTPUT_DIR and no specimen, so without
    this the whole "built" branch — the total, the budget lines, the page
    spans, the enabled "Ansehen" — is executed by nothing at all.
    """
    from jobdeck.services import unterlagen as service
    path = service.specimen_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _blank_pdf(path, pages=total)
    parts, _ = service.anlagen_parts(db.get_setting(con, "anlagen_dir", ""))
    db.set_setting(con, service.LETTER_PAGES_SETTING, str(letter_pages))
    db.set_setting(con, service.ANLAGEN_SETTING, service._anlagen_stamp(parts))
    db.set_setting(con, service.BEFORE_SETTING, str(before))
    db.set_setting(con, service.LOSSLESS_SETTING, "1" if lossless else "0")
    con.commit()
    return path


async def test_a_built_stack_prints_the_page_each_part_starts_on(
        user: User, con, data_dir):
    _posting(con)
    _anlagen(con, data_dir)
    _built_specimen(con, data_dir, letter_pages=3, total=6)

    await user.open("/unterlagen")

    await user.should_see("1–3")   # the letter
    await user.should_see("4–5")   # 01_Zeugnis, two pages
    await user.should_see("6")     # 02_Zertifikat
    await user.should_not_see("Noch nicht gebaut")


async def test_a_built_stack_states_both_budgets(user: User, con, data_dir):
    _posting(con)
    _anlagen(con, data_dir)
    _built_specimen(con, data_dir)
    db.set_setting(con, "mappe_target_mb", "0.000001")  # ~1 byte: over budget
    con.commit()

    await user.open("/unterlagen")

    await user.should_see("E-Mail:")
    await user.should_see("Portal:")


async def test_over_the_german_ceiling_is_said_in_those_words(
        user: User, con, data_dir):
    """If this line is missing or inverted he sends an oversized Mappe
    believing the screen told him it fits."""
    from jobdeck import pdf as pdf_module
    _posting(con)
    _anlagen(con, data_dir)
    _built_specimen(con, data_dir)
    saved = pdf_module.MAX_MAPPE_BYTES
    pdf_module.MAX_MAPPE_BYTES = 10  # the specimen is larger than ten bytes
    try:
        await user.open("/unterlagen")
        await user.should_see("5-MB-Konvention")
    finally:
        pdf_module.MAX_MAPPE_BYTES = saved


async def test_a_budget_that_fits_says_passt_rather_than_warning(
        user: User, con, data_dir):
    _posting(con)
    _anlagen(con, data_dir)
    _built_specimen(con, data_dir)

    await user.open("/unterlagen")

    await user.should_see("passt")
    await user.should_not_see("wird stärker komprimiert")


async def test_with_shrinking_switched_off_nothing_promises_more_compression(
        user: User, con, data_dir):
    """The specimen is already fitted to the e-mail budget, and with
    compression off nothing was fitted at all — "für diesen Weg wird stärker
    komprimiert" was false in both cases. The same bytes are what goes out."""
    _posting(con)
    _anlagen(con, data_dir)
    _built_specimen(con, data_dir)
    db.set_setting(con, "mappe_compress", "0")
    db.set_setting(con, "mappe_target_mb", "0.000001")
    db.set_setting(con, "mappe_target_portal_mb", "0.000001")
    con.commit()

    await user.open("/unterlagen")

    await user.should_see("über dem Budget")
    await user.should_see("ausgeschaltet, es geht so raus")
    await user.should_not_see("wird stärker komprimiert")


async def test_the_weight_is_withheld_when_the_anlagen_moved_since_the_build(
        user: User, con, data_dir):
    """Page numbers stay right — the letter's length was measured — but the
    size belongs to a Mappe that no longer matches this stack."""
    _posting(con)
    folder = _anlagen(con, data_dir)
    _built_specimen(con, data_dir, letter_pages=3, total=6)
    _blank_pdf(folder / "03_Neu.pdf", pages=2)

    await user.open("/unterlagen")

    await user.should_see("Gewicht unbekannt")
    await user.should_see("seit dem letzten Bauen")
    await user.should_see("4–5")  # the Zeugnis did not move


async def test_a_shrunk_mappe_says_so_in_german(user: User, con, data_dir):
    _posting(con)
    _anlagen(con, data_dir)
    _built_specimen(con, data_dir, before=3_850_000, lossless=True)

    await user.open("/unterlagen")

    await user.should_see("verlustfrei von 3,7 MB")


# --------------------------------------------------------------------------
# The consent boundary on the AI proposal
# --------------------------------------------------------------------------
async def test_a_reading_lands_as_proposals_that_count_for_nothing(
        user: User, con, data_dir):
    """The consent boundary of this feature, and where it moved to.

    It used to be a dialog of checkboxes: tick, press Übernehmen, and what
    was ticked became a permission. Nothing survived a closed window — a
    reading he had paid for was simply gone. Now the reading lands, visibly,
    as rows that count for nothing until he answers each one.
    """
    from jobdeck.services import claims as claims_service
    _posting(con)

    async def read():
        db.add_claim(con, {"fact": "FastAPI", "binding": "IHK-Projekt",
                           "terms": "FastAPI", "kind": "skill",
                           "source": "profile_md",
                           "source_ref": "Technische Kenntnisse"})
        db.add_claim(con, {"fact": "Kotlin", "binding": "Praktikum",
                           "terms": "Kotlin", "kind": "skill",
                           "source": "profile_md",
                           "source_ref": "Technische Kenntnisse"})
        con.commit()
        return {"ok": True, "error": "", "written": 2, "skipped": 0,
                "cost_usd": 0.02}

    saved = claims_service.import_from_profile
    claims_service.import_from_profile = read
    try:
        await user.open("/unterlagen")
        user.find(marker="propose-claims").click()
        await asyncio.sleep(0.2)
        user.find(marker="confirm-propose").click()
        await asyncio.sleep(0.4)

        await user.should_see("2 Vorschläge")
        await user.should_see("noch ist keiner davon bestätigt")
        # The proposals sit under a heading that does not contradict them.
        await user.should_not_see("Jede Zeile ist eine")
        # Where each came from, on the row itself — the question the register
        # has to answer about anything it holds.
        await user.should_see("aus profile.md · Technische Kenntnisse")
        assert db.list_claims(con, states=("confirmed",)) == [], (
            "a reading confirmed itself")

        # He refuses the one welded to the wrong employer, keeps the other.
        rejects = _marked(user, "reject-claim")
        assert len(rejects) == 2, "a proposal rendered without a way to refuse"
        assert len(_marked(user, "confirm-claim")) == 2
        user.find(marker="confirm-family-skill").click()
        await asyncio.sleep(0.4)
    finally:
        claims_service.import_from_profile = saved

    assert sorted(r["fact"] for r in db.list_claims(con, states=("confirmed",))
                  ) == ["FastAPI", "Kotlin"]


async def test_a_refused_proposal_leaves_the_register_and_stays_refused(
        user: User, con, data_dir):
    """Refusing is an answer, and the register keeps it — otherwise the next
    reading of the profile offers back exactly what he just said no to."""
    _posting(con)
    db.add_claim(con, {"fact": "C#", "binding": "Eigenprojekt", "terms": "C#",
                       "source": "profile_md", "source_ref": "Kenntnisse"})
    con.commit()

    await user.open("/unterlagen")
    user.find(marker="reject-claim").click()
    await asyncio.sleep(0.4)

    await user.should_not_see("C#")
    rows = db.list_claims(con, states=("rejected",))
    assert [r["fact"] for r in rows] == ["C#"]
    assert rows[0]["confirmed_at"] == "", "a refusal was stamped as consent"


async def test_the_cost_is_stated_before_the_spend_not_after(user: User, con,
                                                             data_dir):
    """It used to be revealed inside the proposal dialog — after it had been
    spent — while the button itself had no confirmation at all."""
    _posting(con)

    await user.open("/unterlagen")
    user.find(marker="propose-claims").click()
    await asyncio.sleep(0.2)

    await user.should_see("profile.md von der KI lesen lassen?")
    # The figure itself, not just "it costs something": the reading now spans
    # every family in his profile rather than his competences alone, so what
    # the button is about to spend has grown with it.
    await user.should_see("zwei Cent")
    assert db.get_setting(con, "llm_calls", "0") == "0"


# --------------------------------------------------------------------------
# Getting documents in — the rubric had no way in at all
# --------------------------------------------------------------------------
async def test_the_screen_names_the_folder_the_documents_come_from(
        user: User, con, data_dir):
    """The stack measured files from a folder it never named, and Einstellungen
    was the only place the path appeared. "I do not understand where I can
    upload my documents" was the literal answer to that."""
    folder = _anlagen(con, data_dir)
    _posting(con)

    await user.open("/unterlagen")

    await user.should_see("Deine Anlagen liegen in")
    await user.should_see(str(folder))
    await user.should_see("Ordner öffnen")


async def test_a_configured_folder_offers_somewhere_to_drop_a_pdf(
        user: User, con, data_dir):
    """`ui.upload` did not appear anywhere in the repository."""
    _anlagen(con, data_dir)
    _posting(con)

    await user.open("/unterlagen")

    assert [el for el in user.client.elements.values()
            if isinstance(el, ui.upload)], "no upload control on the screen"
    await user.should_see("Zeugnisse und Zertifikate hierher ziehen")


async def test_the_upload_says_the_cv_does_not_belong_there(
        user: User, con, data_dir):
    """The Lebenslauf comes out of the letter template. Dropped in here it
    would be merged a second time, and the Mappe an employer opens would carry
    two CVs — with the cover sheet still promising one."""
    _anlagen(con, data_dir)
    _posting(con)

    await user.open("/unterlagen")

    await user.should_see("Der Lebenslauf gehört NICHT hierher")


async def test_without_a_folder_there_is_nothing_to_upload_into(
        user: User, con, data_dir):
    """Offering a drop zone that discards what is dropped is worse than
    offering none: the file looks filed and reaches no employer."""
    _posting(con)
    con.commit()

    await user.open("/unterlagen")

    assert not [el for el in user.client.elements.values()
                if isinstance(el, ui.upload)]


async def test_every_anlage_can_be_moved_and_taken_out(
        user: User, con, data_dir):
    _anlagen(con, data_dir)
    _posting(con)

    await user.open("/unterlagen")

    assert len(_marked(user, "remove-anlage")) == 2, \
        "one per Anlage — and never one for the letter"


async def test_the_letter_row_carries_no_file_controls(
        user: User, con, data_dir):
    """The first part of the stack comes out of the template and has no file
    in the folder. A rename or a removal offered there would act on whichever
    file happened to sit at that position."""
    folder = data_dir / "anlagen"
    folder.mkdir()
    db.set_setting(con, "anlagen_dir", str(folder))
    _posting(con)
    con.commit()

    await user.open("/unterlagen")

    await user.should_see("Deckblatt")           # the letter part is drawn
    assert _marked(user, "remove-anlage") == []


async def test_the_ends_of_the_stack_cannot_be_moved_past(
        user: User, con, data_dir):
    """A disabled arrow states the boundary where it is; an enabled one that
    quietly does nothing teaches him the control is broken."""
    folder = data_dir / "anlagen"
    folder.mkdir()
    for name in ("01_A.pdf", "02_B.pdf", "03_C.pdf"):
        _blank_pdf(folder / name)
    db.set_setting(con, "anlagen_dir", str(folder))
    _posting(con)
    con.commit()

    await user.open("/unterlagen")

    assert [_marked(user, f"anlage-up-{n}")[0].enabled for n in range(3)] == \
        [False, True, True]
    assert [_marked(user, f"anlage-down-{n}")[0].enabled for n in range(3)] == \
        [True, True, False]


async def test_taking_an_anlage_out_asks_first_and_says_the_file_survives(
        user: User, con, data_dir):
    """This is the one control that could read as "delete my Prüfungszeugnis",
    and the answer to that belongs in the question."""
    folder = data_dir / "anlagen"
    folder.mkdir()
    _blank_pdf(folder / "01_Zeugnis.pdf")
    db.set_setting(con, "anlagen_dir", str(folder))
    _posting(con)
    con.commit()

    await user.open("/unterlagen")
    user.find(marker="remove-anlage").click()

    await user.should_see("aus der Mappe nehmen?")
    await user.should_see("Die Datei wird nicht gelöscht")
    # nothing has happened yet — the question is the gate, not a report
    assert (folder / "01_Zeugnis.pdf").exists()


async def test_cancelling_leaves_the_mappe_exactly_as_it_was(
        user: User, con, data_dir):
    folder = data_dir / "anlagen"
    folder.mkdir()
    _blank_pdf(folder / "01_Zeugnis.pdf")
    db.set_setting(con, "anlagen_dir", str(folder))
    _posting(con)
    con.commit()

    await user.open("/unterlagen")
    user.find(marker="remove-anlage").click()
    await user.should_see("aus der Mappe nehmen?")
    user.find("Abbrechen").click()

    assert (folder / "01_Zeugnis.pdf").exists()
    assert list(anlagen_service.trash_dir().glob("*.pdf")) == []


async def test_confirming_takes_it_out_of_the_mappe_and_keeps_the_file(
        user: User, con, data_dir):
    folder = data_dir / "anlagen"
    folder.mkdir()
    _blank_pdf(folder / "01_Zeugnis.pdf")
    db.set_setting(con, "anlagen_dir", str(folder))
    _posting(con)
    con.commit()

    await user.open("/unterlagen")
    user.find(marker="remove-anlage").click()
    await user.should_see("aus der Mappe nehmen?")
    user.find(marker="confirm-remove-anlage").click()
    await user.should_see("liegt jetzt in")

    assert not (folder / "01_Zeugnis.pdf").exists()
    assert [p.name for p in anlagen_service.trash_dir().glob("*.pdf")] == \
        ["01_Zeugnis.pdf"]
    # and the screen has caught up: the row is gone with it
    assert _marked(user, "remove-anlage") == []


async def test_the_arrow_really_reorders_the_files(user: User, con, data_dir):
    """Driven through the button rather than the service: what `run.io_bound`
    hands a worker has to be callable with what it hands it, and a TypeError
    there is a log line and a control that silently does nothing."""
    folder = data_dir / "anlagen"
    folder.mkdir()
    _blank_pdf(folder / "01_Zeugnis.pdf")
    _blank_pdf(folder / "02_Zertifikat.pdf")
    db.set_setting(con, "anlagen_dir", str(folder))
    _posting(con)
    con.commit()

    await user.open("/unterlagen")
    user.find(marker="anlage-down-0").click()
    await user.should_see("02_Zeugnis")

    assert sorted(p.name for p in folder.glob("*.pdf")) == [
        "01_Zertifikat.pdf", "02_Zeugnis.pdf"]


# --------------------------------------------------------------------------
# What the workers answer — an exception here is a log line and a dead button
# --------------------------------------------------------------------------
def test_a_refused_upload_is_answered_not_raised(data_dir):
    folder = data_dir / "anlagen"
    folder.mkdir()

    result = unterlagen_page._add_anlage(str(folder), "Lebenslauf.docx", b"x")

    assert result["ok"] is False
    assert "Nur PDF" in result["error"]
    assert list(folder.iterdir()) == []


def test_a_refused_move_is_answered_not_raised(data_dir):
    folder = data_dir / "anlagen"
    folder.mkdir()

    result = unterlagen_page._move_anlage(str(folder), "01_gone.pdf", 1)

    assert result["ok"] is False
    assert "nicht mehr im Ordner" in result["error"]


def test_a_refused_removal_is_answered_not_raised(data_dir):
    folder = data_dir / "anlagen"
    folder.mkdir()

    result = unterlagen_page._remove_anlage(str(folder), "../escape.pdf")

    assert result["ok"] is False
    assert "Ungültiger Dateiname" in result["error"]


def test_adopting_a_folder_creates_it_and_points_the_setting_at_it(con, data_dir):
    """The one press that answers "where do I put my documents" without a trip
    to Einstellungen."""
    wanted = data_dir / "Anlagen"

    result = unterlagen_page._adopt_folder(str(wanted))

    assert result["ok"] is True
    assert wanted.is_dir()
    with db.db() as fresh:
        assert db.get_setting(fresh, "anlagen_dir", "") == str(wanted)


def test_adopting_repairs_a_folder_that_was_moved_away(con, data_dir):
    """Same button, second job: the setting already names a path that is gone,
    and re-creating THAT path is the repair — silently pointing him somewhere
    else would leave his certificates behind."""
    gone = data_dir / "woanders" / "Anlagen"
    db.set_setting(con, "anlagen_dir", str(gone))
    con.commit()

    result = unterlagen_page._adopt_folder(str(gone))

    assert result["ok"] is True and gone.is_dir()
    with db.db() as fresh:
        assert db.get_setting(fresh, "anlagen_dir", "") == str(gone)


async def test_the_up_arrow_moves_a_document_towards_the_front(
        user: User, con, data_dir):
    """"Nach vorn" with an inverted delta renames on disk and puts a course
    certificate ahead of the Prüfungszeugnis in what an employer opens. The
    direction is one character and no test held it."""
    folder = data_dir / "anlagen"
    folder.mkdir()
    _blank_pdf(folder / "01_Zeugnis.pdf")
    _blank_pdf(folder / "02_Zertifikat.pdf")
    db.set_setting(con, "anlagen_dir", str(folder))
    _posting(con)
    con.commit()

    await user.open("/unterlagen")
    user.find(marker="anlage-up-1").click()
    await user.should_see("01_Zertifikat")

    assert sorted(p.name for p in folder.glob("*.pdf")) == [
        "01_Zertifikat.pdf", "02_Zeugnis.pdf"]


async def test_a_second_arrow_press_mid_move_cannot_move_the_wrong_document(
        user: User, con, data_dir):
    """The arrows RENAME, so between the press and the redraw the name in the
    next button's closure is already stale — and after a swap that name
    belongs to a different certificate. Two nudges in a row is the ordinary
    way to move something two places."""
    folder = data_dir / "anlagen"
    folder.mkdir()
    for name in ("01_A.pdf", "02_B.pdf", "03_C.pdf"):
        _blank_pdf(folder / name)
    db.set_setting(con, "anlagen_dir", str(folder))
    _posting(con)
    con.commit()

    await user.open("/unterlagen")
    user.find(marker="anlage-down-0").click()
    user.find(marker="anlage-down-0").click()      # inside the redraw window
    await user.should_see("01_B")

    assert sorted(p.name for p in folder.glob("*.pdf")) == [
        "01_B.pdf", "02_A.pdf", "03_C.pdf"], "the second press acted on a " \
        "name the first had already rewritten"
    await user.should_not_see("nicht mehr im Ordner")


async def test_the_first_press_of_all_creates_the_folder_and_uses_it(
        user: User, con, data_dir):
    """The one press the whole rubric exists to provide, for somebody who has
    never opened Einstellungen."""
    _posting(con)
    con.commit()

    await user.open("/unterlagen")
    await user.should_see("Noch kein Ordner für deine Anlagen")
    user.find("Ordner anlegen und verwenden").click()
    await user.should_see("Ordner angelegt")

    assert (data_dir / "Anlagen").is_dir()
    with db.db() as fresh:
        assert db.get_setting(fresh, "anlagen_dir", "") == \
            str(data_dir / "Anlagen")
    # …and the screen has become the one that can take a document
    await user.should_see("Deine Anlagen liegen in")
    assert [el for el in user.client.elements.values()
            if isinstance(el, ui.upload)]


# --------------------------------------------------------------------------
# The drop zone, driven. The slice's headline path had no execution coverage
# at all: a NiceGUI rename, or an early return, would leave the suite green
# while every dropped file vanished — which is the complaint it exists to fix.
# --------------------------------------------------------------------------
def _uploader(user: User):
    with user.client:
        return next(el for el in user.client.elements.values()
                    if isinstance(el, ui.upload))


def _files(*specs) -> list:
    """(name, bytes) pairs as NiceGUI's own upload payload."""
    return [ui.upload.SmallFileUpload(name=name, content_type="application/pdf",
                                      _data=data)
            for name, data in specs]


async def _drop(user: User, *specs) -> None:
    await _uploader(user).handle_uploads(_files(*specs))
    await asyncio.sleep(0.1)


async def test_a_dropped_pdf_really_reaches_the_folder(user: User, con, data_dir):
    folder = _anlagen(con, data_dir)

    await user.open("/unterlagen")
    await _drop(user, ("Sprachzertifikat B2.pdf", _pdf_bytes()))

    assert (folder / "03_Sprachzertifikat_B2.pdf").exists()
    await user.should_see("liegt jetzt in der Mappe")
    await user.should_see("03_Sprachzertifikat_B2")     # and on the stack


async def test_dropping_six_certificates_at_once_keeps_all_six(
        user: User, con, data_dir):
    """The natural first action on a drop zone that says "hierher ziehen".
    Redrawing after each file unmounted the uploader and aborted the rest of
    the transfer — one green toast, five documents gone, nothing said."""
    folder = _anlagen(con, data_dir)

    await user.open("/unterlagen")
    await _drop(user, *[(f"Zertifikat {n}.pdf", _pdf_bytes()) for n in range(1, 7)])

    assert sorted(p.name for p in folder.glob("*.pdf")) == [
        "01_Zeugnis.pdf", "02_Zertifikat.pdf",
        "03_Zertifikat_1.pdf", "04_Zertifikat_2.pdf", "05_Zertifikat_3.pdf",
        "06_Zertifikat_4.pdf", "07_Zertifikat_5.pdf", "08_Zertifikat_6.pdf"]
    await user.should_see("6 Anlagen liegen jetzt in der Mappe")


async def test_one_bad_file_in_a_batch_is_named_and_the_rest_still_land(
        user: User, con, data_dir):
    """"Three of five arrived" is useless if it does not say which two did
    not — he would have to compare the folder against his own memory."""
    folder = _anlagen(con, data_dir)

    await user.open("/unterlagen")
    await _drop(user,
                ("Gut.pdf", _pdf_bytes()),
                ("Zerrissen.pdf", b"%PDF-1.4\nbroken"),
                ("AuchGut.pdf", _pdf_bytes()))

    assert (folder / "03_Gut.pdf").exists()
    assert (folder / "04_AuchGut.pdf").exists()
    assert not list(folder.glob("*Zerrissen*"))
    await user.should_see("Zerrissen.pdf")
    await user.should_see("Kein lesbares PDF")
    await user.should_see("2 Anlagen liegen jetzt in der Mappe")


async def test_a_dropped_word_document_is_refused_with_its_name(
        user: User, con, data_dir):
    folder = _anlagen(con, data_dir)

    await user.open("/unterlagen")
    await _drop(user, ("Lebenslauf.docx", b"PK\x03\x04"))

    await user.should_see("Nur PDF")
    assert sorted(p.name for p in folder.glob("*")) == [
        "01_Zeugnis.pdf", "02_Zertifikat.pdf"]


async def test_an_oversized_file_is_refused_before_it_is_read(
        user: User, con, data_dir):
    """The limit exists so the file is never held in memory; asking after
    reading it would be a limit that had already been exceeded."""
    _anlagen(con, data_dir)

    await user.open("/unterlagen")
    huge = _pdf_bytes() + b"\x00" * anlagen_service.MAX_UPLOAD_BYTES
    await _drop(user, ("Riesig.pdf", huge))

    await user.should_see("Zu groß")


async def test_a_failed_upload_still_redraws_the_screen(
        user: User, con, data_dir):
    """The deferral flag holds the screen still during the transfer, and it is
    lowered in a `finally`. Left raised by a failure it would freeze the page's
    self-refresh for the rest of its life — the exact staleness the live
    watcher exists to end — and the redraw after the failure would be skipped
    too, leaving the stack describing a folder that has moved on."""
    folder = _anlagen(con, data_dir)
    await user.open("/unterlagen")
    _blank_pdf(folder / "03_Nachgereicht.pdf")     # arrives outside the app

    await _drop(user, ("Zerrissen.pdf", b"%PDF-1.4\nbroken"))

    await user.should_see("Kein lesbares PDF")
    await user.should_see("03_Nachgereicht")       # the redraw happened anyway


async def test_the_drop_zone_is_wired_the_one_way_that_does_not_race(
        user: User, con, data_dir):
    """Read out of NiceGUI's own source, and easy to undo by accident.

    `handle_event` schedules an async handler as a BACKGROUND TASK rather than
    awaiting it, so `on_upload` and `on_multi_upload` do not run in order — a
    "store each file, refresh at the end" split would race its own refresh past
    the last file. And Quasar sends one POST per file unless `batch` is set,
    which would fire the whole cycle once per file. One handler, one request:
    anything else silently drops files."""
    _anlagen(con, data_dir)
    await user.open("/unterlagen")

    uploader = _uploader(user)
    assert uploader.props.get("batch"), "one POST per file races the redraw"
    assert len(uploader._multi_upload_handlers) == 1
    assert uploader._upload_handlers == [], \
        "a per-file handler runs unordered against the batch handler"
    assert uploader._begin_upload_handlers, \
        "without this the screen is only held still AFTER the transfer"


async def test_a_waiting_proposal_carries_the_count_it_really_has(
        user: User, con, data_dir):
    """It used to read "noch kein Wort davon" — a count nobody performed.

    Found by driving the real screen: one fact said that while it waited and
    "in 50 Briefen" one click later, with nothing about the letters having
    changed. A proposal is read out of the profile the letters are ALREADY
    written from, so how often they claim it is both true and the strongest
    reason to confirm it.
    """
    job_id = _posting(con)
    _letter(con, job_id, "… FastAPI im Abschlussprojekt …")
    db.add_claim(con, {"fact": "FastAPI", "binding": "IHK-Projekt",
                       "terms": "FastAPI", "source": "profile_md",
                       "source_ref": "Technische Kenntnisse"})
    con.commit()

    await user.open("/unterlagen")

    await user.should_see("in 1 Brief")
    await user.should_not_see("noch kein Wort davon")


async def test_a_hand_written_entry_can_choose_and_change_its_family(
        user: User, con, data_dir):
    """Without a family control every hand-typed fact was filed as a
    competence: "Englisch, verhandlungssicher" stood under "Technische
    Kenntnisse", and seven of the eight families the register knows were
    unreachable except through the AI reading — which could not be corrected
    either, because the same dialog is the only edit path."""
    from nicegui import ui as nicegui_ui
    _posting(con)

    await user.open("/unterlagen")
    user.find("Erlaubnis hinzufügen").click()
    await asyncio.sleep(0.2)

    with user.client:
        family = next(e for e in user.client.elements.values()
                      if isinstance(e, nicegui_ui.select))
        inputs = [e for e in user.client.elements.values()
                  if isinstance(e, nicegui_ui.input)]
    family.set_value("language")
    inputs[0].set_value("Englisch, verhandlungssicher")
    inputs[2].set_value("Englisch")
    await asyncio.sleep(0.1)
    user.find("Speichern").click()
    await asyncio.sleep(0.4)

    row = db.list_claims(con)[0]
    assert row["kind"] == "language", "the family he chose was not stored"
    assert row["state"] == "confirmed"
    # The heading's capitals are CSS; the label itself is the German word.
    await user.should_see("Sprachen")


async def test_a_refused_claim_has_a_door_and_a_way_back(user: User, con,
                                                         data_dir):
    """Refusing is one click on an icon beside its identical opposite, and a
    refused row leaves every view AND stops a later reading proposing it
    again. Without a way back the cost of a mis-click is the fact itself,
    discoverable only by paying for a second reading that reports nothing
    new."""
    _posting(con)
    claim_id = db.add_claim(con, {"fact": "FastAPI, PostgreSQL", "terms": "FastAPI",
                                  "binding": "IHK-Projekt",
                                  "source": "profile_md",
                                  "source_ref": "Technische Kenntnisse"})
    con.commit()

    await user.open("/unterlagen")
    user.find(marker="reject-claim").click()
    await asyncio.sleep(0.4)
    await user.should_not_see("FastAPI, PostgreSQL")

    # A number under a list has to be a door.
    await user.should_see("1 abgelehnt")
    user.find(marker="toggle-refused").click()
    await asyncio.sleep(0.3)
    await user.should_see("FastAPI, PostgreSQL")

    user.find(marker="restore-claim").click()
    await asyncio.sleep(0.4)

    row = db.list_claims(con)[0]
    assert row["id"] == claim_id and row["state"] == "proposed"
    assert _marked(user, "confirm-claim"), "the restored row cannot be answered"


async def test_the_refused_door_is_absent_when_nothing_was_refused(
        user: User, con, data_dir):
    _posting(con)
    db.add_claim(con, {"fact": "Django", "binding": "Praktikum",
                       "state": "confirmed"})
    con.commit()

    await user.open("/unterlagen")

    assert _marked(user, "toggle-refused") == []
    await user.should_not_see("abgelehnt")
