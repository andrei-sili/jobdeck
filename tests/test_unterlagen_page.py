"""The Unterlagen screen, rendered for real.

What it must state is testable only by drawing it: the stack has to name the
pages an employer turns, the letter head has to name the fields an application
would leave blank, and the register has to be honest about the one thing it
does not do yet.
"""

import asyncio
import pathlib
import sys

import pytest
from nicegui import ui
from nicegui.testing import User
from pypdf import PdfWriter

from jobdeck import db

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
    # nothing has been built yet, so the total is not claimed
    await user.should_see("Noch nicht gebaut")


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
    _posting(con)
    db.set_setting(con, "anlagen_dir", str(data_dir / "gibt-es-nicht"))
    con.commit()

    await user.open("/unterlagen")

    await user.should_see("does not exist")


async def test_the_order_of_the_stack_says_where_it_comes_from(
        user: User, con, data_dir):
    """The filenames are the single source of that order. A screen that let it
    be set somewhere else too would have two, and they would drift."""
    _posting(con)
    _anlagen(con, data_dir)

    await user.open("/unterlagen")

    await user.should_see("Umbenennen ordnet um")


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
                       "terms": "FastAPI"})
    db.add_claim(con, {"fact": "Java & Spring Boot", "binding": "Eigenprojekt",
                       "terms": "Spring Boot"})
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
    db.add_claim(con, {"fact": "Django & DRF", "binding": "Praktikum"})
    con.commit()

    await user.open("/unterlagen")

    await user.should_see("nicht zählbar")


async def test_the_register_says_it_does_not_yet_constrain_the_prompt(
        user: User, con, data_dir):
    """The mirror-first decision, pinned. The drafting prompt still reads
    profile.md; a screen implying otherwise would be believed at exactly the
    moment a letter goes out."""
    _posting(con)
    await user.open("/unterlagen")

    await user.should_see("profile.md")


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


async def test_deleting_a_permission_asks_first(user: User, con, data_dir):
    _posting(con)
    claim_id = db.add_claim(con, {"fact": "Java", "binding": "Eigenprojekt"})
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
