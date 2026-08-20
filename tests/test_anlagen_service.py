"""The Anlagen folder as something the app writes to.

Until this module there was no way into it at all: documents entered only by
typing a path into Einstellungen and dropping files there by hand. These tests
are about the three ways writing into a folder of scanned originals can go
quietly wrong — a file that lands but can never be merged, a reorder that
renames one certificate onto another, and a removal that is a deletion.
"""

import io
import pathlib

import pytest
from pypdf import PdfReader, PdfWriter

from jobdeck import pdf
from jobdeck.services import anlagen


def _pdf_bytes(pages: int = 1, *, password: str = "") -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if password:
        writer.encrypt(password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _folder(tmp_path: pathlib.Path, *names: str) -> pathlib.Path:
    folder = tmp_path / "Anlagen"
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(_pdf_bytes())
    return folder


# ---------------------------------------------------------------- storing


def test_an_upload_lands_at_the_end_of_his_numbering(tmp_path):
    """A curated folder is in the order a recruiter leafs through it — the
    examination certificate before the course certificates. A new document has
    no claim on a position the user chose, so it goes last, and he moves it up
    if it belongs up."""
    folder = _folder(tmp_path, "01_Zeugnis.pdf", "02_Praktikum.pdf")

    stored = anlagen.store(folder, "Zertifikat.pdf", _pdf_bytes())

    assert stored.name == "03_Zertifikat.pdf"
    assert [p.name for p in pdf.collect_anlagen(str(folder))] == [
        "01_Zeugnis.pdf", "02_Praktikum.pdf", "03_Zertifikat.pdf"]


def test_the_first_document_of_all_is_numbered_one(tmp_path):
    folder = _folder(tmp_path)

    assert anlagen.store(folder, "Zeugnis.pdf", _pdf_bytes()).name == \
        "01_Zeugnis.pdf"


def test_an_already_numbered_upload_is_not_numbered_twice(tmp_path):
    """He names his own files 01_, 02_ … so the file he picks in the dialog
    very often already carries one. "07_01_Zeugnis" would then be sorted by a
    number he did not choose and read as a mistake by the app."""
    folder = _folder(tmp_path, "01_Zeugnis.pdf")

    stored = anlagen.store(folder, "05_Praktikum.pdf", _pdf_bytes())

    assert stored.name == "02_Praktikum.pdf"


def test_a_second_copy_of_one_name_does_not_replace_the_first(tmp_path):
    folder = _folder(tmp_path)
    first = anlagen.store(folder, "Zeugnis.pdf", _pdf_bytes(pages=1))

    second = anlagen.store(folder, "Zeugnis.pdf", _pdf_bytes(pages=3))

    assert first.name == "01_Zeugnis.pdf" and second.name == "02_Zeugnis.pdf"
    assert first.exists()
    assert len(PdfReader(str(first)).pages) == 1
    assert len(PdfReader(str(second)).pages) == 3


def test_a_word_document_is_refused_rather_than_silently_ignored(tmp_path):
    """`collect_anlagen` filters on the .pdf suffix, so a .docx dropped in
    here would sit in the folder looking filed and never reach an employer."""
    folder = _folder(tmp_path)

    with pytest.raises(anlagen.AnlagenError, match="Nur PDF"):
        anlagen.store(folder, "Lebenslauf.docx", b"PK\x03\x04 not a pdf")

    assert list(folder.iterdir()) == []


def test_a_torn_pdf_is_refused_at_the_door(tmp_path):
    """A truncated download passes a name check and a magic-byte check. It
    surfaces later as "nicht lesbar" on the stack — or, unread, as a failed
    build in the middle of applying."""
    folder = _folder(tmp_path)

    with pytest.raises(anlagen.AnlagenError, match="Kein lesbares PDF"):
        anlagen.store(folder, "Zeugnis.pdf", b"%PDF-1.4\nbroken")

    assert list(folder.iterdir()) == []


def test_a_password_protected_pdf_says_so_rather_than_calling_it_broken(tmp_path):
    """Protected and corrupt need different answers: one is re-saved without
    the protection, the other is scanned again."""
    folder = _folder(tmp_path)

    with pytest.raises(anlagen.AnlagenError, match="passwortgeschützt"):
        anlagen.store(folder, "Zeugnis.pdf", _pdf_bytes(password="geheim"))

    assert list(folder.iterdir()) == []


def test_an_oversized_file_is_refused_with_its_size(tmp_path):
    folder = _folder(tmp_path)
    huge = _pdf_bytes() + b"\x00" * anlagen.MAX_UPLOAD_BYTES

    with pytest.raises(anlagen.AnlagenError, match="Zu groß"):
        anlagen.store(folder, "Zeugnis.pdf", huge)

    assert list(folder.iterdir()) == []


def test_an_empty_upload_is_refused(tmp_path):
    folder = _folder(tmp_path)

    with pytest.raises(anlagen.AnlagenError, match="leer"):
        anlagen.store(folder, "Zeugnis.pdf", b"")


def test_a_filename_from_the_browser_cannot_escape_the_folder(tmp_path):
    """The name travels through a browser and is joined onto a path here."""
    folder = _folder(tmp_path)

    stored = anlagen.store(folder, "../../evil.pdf", _pdf_bytes())

    assert stored.parent == folder
    assert stored.name == "01_evil.pdf"
    assert not (tmp_path.parent / "evil.pdf").exists()


def test_nothing_half_written_is_ever_merged(tmp_path):
    """The staging name deliberately does not end in .pdf: a build running
    while the browser is still uploading must not merge half a file. Built
    from the constant, so changing the constant to something the merge picks
    up turns this red instead of leaving it true about a name nobody uses."""
    folder = _folder(tmp_path)
    (folder / f"02_Zeugnis.pdf{anlagen._STAGE_SUFFIX}").write_bytes(b"half")

    assert pdf.collect_anlagen(str(folder)) == []
    assert anlagen.listing(folder) == []


def test_a_pdf_with_no_pages_is_refused(tmp_path):
    """A structurally valid PDF that contributes nothing. It would merge
    without error and appear on the stack as a part with no pages — a row
    naming a document an employer never sees."""
    folder = _folder(tmp_path)
    empty = PdfWriter()
    buffer = io.BytesIO()
    empty.write(buffer)

    with pytest.raises(anlagen.AnlagenError, match="keine Seiten"):
        anlagen.store(folder, "Zeugnis.pdf", buffer.getvalue())

    assert list(folder.iterdir()) == []


# --------------------------------------------------------------- ordering


def test_the_screen_lists_exactly_what_the_merge_will_staple(tmp_path):
    """The one differential that matters: if these two ever disagree, the
    screen describes a document nobody receives."""
    folder = _folder(tmp_path, "02_B.pdf", "01_A.pdf", "10_J.pdf", "9_I.pdf",
                     "C.pdf")
    (folder / "notes.txt").write_bytes(b"x")

    order = [e.name for e in anlagen.listing(folder)]

    assert order == [p.name for p in pdf.collect_anlagen(str(folder))]
    # And spelled out, because the interesting half is where a filename sort
    # and a "sort by the number a human reads" disagree: unpadded 9_ comes
    # AFTER 10_ in the merge, and the screen has to say so rather than show
    # the order he meant.
    assert order == ["01_A.pdf", "02_B.pdf", "10_J.pdf", "9_I.pdf", "C.pdf"]


def test_moving_one_down_swaps_it_with_its_neighbour(tmp_path):
    folder = _folder(tmp_path, "01_A.pdf", "02_B.pdf", "03_C.pdf")

    anlagen.move(folder, "01_A.pdf", 1)

    assert [e.name for e in anlagen.listing(folder)] == [
        "01_B.pdf", "02_A.pdf", "03_C.pdf"]


def test_moving_one_up_swaps_it_with_its_neighbour(tmp_path):
    folder = _folder(tmp_path, "01_A.pdf", "02_B.pdf", "03_C.pdf")

    anlagen.move(folder, "03_C.pdf", -1)

    assert [e.name for e in anlagen.listing(folder)] == [
        "01_A.pdf", "02_C.pdf", "03_B.pdf"]


def test_a_move_renames_only_what_has_to_move(tmp_path):
    """Renaming six certificates because he nudged two of them makes his file
    manager show six changed files, and every one of those is a document he
    might be looking for by name."""
    folder = _folder(tmp_path, "01_A.pdf", "02_B.pdf", "03_C.pdf", "04_D.pdf")
    before = {p.name: p.stat().st_ino for p in folder.iterdir()}

    anlagen.move(folder, "01_A.pdf", 1)

    after = {p.name for p in folder.iterdir()}
    assert after - set(before) == {"01_B.pdf", "02_A.pdf"}
    assert {"03_C.pdf", "04_D.pdf"} <= after


def test_a_move_between_two_files_of_the_same_name_loses_neither(tmp_path):
    """Two uploads of one document are "01_Zeugnis" and "02_Zeugnis" — the
    same human half, different numbers. Swapping them is the case where a
    one-phase rename puts the second onto the first's name and os.replace
    destroys it silently. Every other pair survives a one-phase pass by luck.
    """
    folder = _folder(tmp_path)
    anlagen.store(folder, "Zeugnis.pdf", _pdf_bytes(pages=1))
    anlagen.store(folder, "Zeugnis.pdf", _pdf_bytes(pages=3))

    anlagen.move(folder, "01_Zeugnis.pdf", 1)

    order = anlagen.listing(folder)
    assert [e.name for e in order] == ["01_Zeugnis.pdf", "02_Zeugnis.pdf"]
    assert [len(PdfReader(str(folder / e.name)).pages) for e in order] == [3, 1]


def test_a_move_at_the_end_changes_nothing(tmp_path):
    folder = _folder(tmp_path, "01_A.pdf", "02_B.pdf")
    before = sorted(p.name for p in folder.iterdir())

    anlagen.move(folder, "02_B.pdf", 1)
    anlagen.move(folder, "01_A.pdf", -1)

    assert sorted(p.name for p in folder.iterdir()) == before


def test_a_move_never_loses_a_document(tmp_path):
    """A one-phase rename would put a file onto a name its neighbour still
    holds, and os.replace destroys that neighbour without a word."""
    folder = _folder(tmp_path, "01_A.pdf", "02_B.pdf", "03_C.pdf")

    for _ in range(6):
        anlagen.move(folder, anlagen.listing(folder)[0].name, 1)

    stems = {anlagen.split_prefix(e.stem)[1] for e in anlagen.listing(folder)}
    assert stems == {"A", "B", "C"}


def test_a_move_numbers_files_that_never_had_a_number(tmp_path):
    """A folder he filled before this feature existed can hold bare names;
    they sort alphabetically and cannot be reordered until they are numbered."""
    folder = _folder(tmp_path, "Zeugnis.pdf", "Praktikum.pdf")

    anlagen.move(folder, "Zeugnis.pdf", -1)

    assert [e.name for e in anlagen.listing(folder)] == [
        "01_Zeugnis.pdf", "02_Praktikum.pdf"]


def test_moving_a_file_that_is_gone_says_so(tmp_path):
    """The folder is shared with his file manager — a row on screen can name
    a file he deleted himself a second ago."""
    folder = _folder(tmp_path, "01_A.pdf")

    with pytest.raises(anlagen.AnlagenError, match="nicht mehr im Ordner"):
        anlagen.move(folder, "09_ghost.pdf", -1)


@pytest.mark.parametrize("name", ["../01_A.pdf", "sub/01_A.pdf",
                                  "/etc/passwd", "", ".", "..", "a\x00b.pdf"])
def test_a_move_cannot_be_pointed_outside_the_folder(tmp_path, name):
    """`..` is the one that needs its own guard: `folder / ".."` has `folder`
    as its parent, so the "is it a direct member" check passes it."""
    folder = _folder(tmp_path, "01_A.pdf")

    with pytest.raises(anlagen.AnlagenError, match="Ungültiger Dateiname"):
        anlagen.move(folder, name, 1)


# --------------------------------------------------------------- removing


def test_removing_an_anlage_keeps_the_file(data_dir):
    """These are scanned originals. The app takes a document out of the merge
    order; it is not the thing that loses somebody's Prüfungszeugnis."""
    folder = _folder(data_dir, "01_A.pdf", "02_B.pdf")
    payload = (folder / "01_A.pdf").read_bytes()

    landed = anlagen.remove(folder, "01_A.pdf")

    assert landed.parent == anlagen.trash_dir()
    assert landed.read_bytes() == payload
    assert [e.name for e in anlagen.listing(folder)] == ["02_B.pdf"]


def test_the_discard_pile_is_outside_his_folder(data_dir):
    """Inside it, a removed file is the next thing a build picks up or the
    file picker offers back."""
    folder = _folder(data_dir, "01_A.pdf")

    landed = anlagen.remove(folder, "01_A.pdf")

    assert folder not in landed.parents
    assert pdf.collect_anlagen(str(folder)) == []


def test_removing_the_same_name_twice_keeps_both(data_dir):
    folder = _folder(data_dir, "01_A.pdf")
    first = anlagen.remove(folder, "01_A.pdf")
    (folder / "01_A.pdf").write_bytes(_pdf_bytes(pages=2))

    second = anlagen.remove(folder, "01_A.pdf")

    assert first.exists() and second.exists() and first != second
    assert len(PdfReader(str(second)).pages) == 2


def test_removing_refuses_a_name_that_is_a_path(data_dir):
    """This function deletes from the user's own documents folder."""
    folder = _folder(data_dir, "01_A.pdf")
    outside = data_dir / "keep.pdf"
    outside.write_bytes(_pdf_bytes())

    for name in ("../keep.pdf", "sub/keep.pdf", str(outside), "", ".", "..",
                 "a\x00b.pdf"):
        with pytest.raises(anlagen.AnlagenError, match="Ungültiger Dateiname"):
            anlagen.remove(folder, name)

    assert outside.exists()
    assert (folder / "01_A.pdf").exists()


def test_removing_something_already_gone_says_so(data_dir):
    folder = _folder(data_dir)

    with pytest.raises(anlagen.AnlagenError, match="nicht mehr im Ordner"):
        anlagen.remove(folder, "01_A.pdf")


# ---------------------------------------------------------------- resolve


def test_no_folder_configured_and_a_missing_folder_are_different_states(tmp_path):
    """One needs a folder chosen, the other needs it found. Collapsing them
    into "no Anlagen" is how an empty stack looks correct."""
    assert anlagen.resolve("") is None
    assert anlagen.resolve("   ") is None
    assert anlagen.resolve(str(tmp_path / "gone")) == tmp_path / "gone"


def test_a_configured_folder_that_is_not_there_lists_nothing_rather_than_raising(tmp_path):
    assert anlagen.listing(tmp_path / "gone") == []


def test_the_default_folder_lives_in_the_data_dir(data_dir):
    assert anlagen.default_dir() == data_dir / "Anlagen"
