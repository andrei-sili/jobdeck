"""The Anlagen folder, as something the app can write to.

Until now documents entered JobDeck one way only: the user typed a folder path
into Einstellungen and put files there himself, numbered ``01_``, ``02_`` … The
screen that measures the Mappe never named that folder and offered no way in,
so the honest answer to "where do I upload my documents" was "you cannot" — and
an empty folder drew a Mappe consisting of the letter alone, which looks
correct.

This module is the one place that writes into that folder. Three properties it
exists to hold:

* **The user's folder, not ours.** The path stays whatever ``anlagen_dir``
  says — typically a folder outside the app that the user has curated by hand,
  numbering the files to fix the order. An upload lands beside what is already
  there, keeps that convention going, and the folder stays something a file
  manager opens. A folder the app took ownership of would orphan every
  certificate already collected.
* **Only what the merge can actually use.** ``pdf.collect_anlagen`` picks up
  ``.pdf`` and nothing else, so a Word document dropped here would vanish
  without a word. Anything that is not a readable PDF is refused with the
  reason, before it reaches the folder.
* **Removing is not deleting.** These are scanned originals of certificates.
  "Entfernen" moves the file out of the merge order into
  ``Anlagen-entfernt/``; nothing in this app unlinks a document of his.
"""

import dataclasses
import io
import logging
import os
import pathlib
import re
import shutil

from pypdf import PdfReader

from jobdeck import config, pdf

log = logging.getLogger(__name__)

# A single attachment larger than this is not a certificate, it is a mistake —
# and the whole Mappe has to fit the German 5 MB convention after compression.
# Set well above the largest real one (0,8 MB) so a 600-dpi scan he has not got
# round to shrinking still goes in and gets compressed on the way out.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# "01_Zeugnis_2026" -> (1, "Zeugnis_2026"). A separator is
# REQUIRED after the digits: without it "2026_Zeugnis" would be read as number
# 202 of something called "6_Zeugnis", and the renumbering would then rewrite a
# file name that was never an order marker at all.
_PREFIX_RE = re.compile(r"^(\d{1,3})(?=[_\-. ])[_\-. ]+(.*)$")

# Renames go through this so a swap never has two files claiming one name.
# The suffix is deliberately not .pdf: `collect_anlagen` filters on the
# suffix, so a file caught mid-rename is invisible to a build rather than
# merged at the wrong position.
_STAGE_SUFFIX = ".jd-move"


class AnlagenError(Exception):
    """Refusal with a reason the user reads, in German."""


@dataclasses.dataclass(frozen=True)
class Entry:
    """One file in the folder, in merge order."""

    name: str          # file name including suffix — the handle for every op
    stem: str          # what the stack labels it
    number: int | None  # the numeric prefix, or None if it has none
    size_bytes: int


def default_dir() -> pathlib.Path:
    """Where a first-time user's Anlagen go when no folder is configured.

    Inside the data directory, because that is the one place this app may
    create things without asking; a user who already keeps documents elsewhere
    points the setting at his own folder and this is never used.
    """
    return pathlib.Path(config.DATA_DIR) / "Anlagen"


def trash_dir() -> pathlib.Path:
    """Where a removed Anlage goes. Beside the data, never inside his folder:
    a hidden pile of discards inside the curated folder is the next thing that
    gets re-uploaded by mistake."""
    return pathlib.Path(config.DATA_DIR) / "Anlagen-entfernt"


def resolve(anlagen_dir: str) -> pathlib.Path | None:
    """The configured folder as a path, or None when nothing is configured.

    None and "configured but missing" are different states with different
    answers — one needs a folder chosen, the other needs it found — so this
    does not paper over the difference by inventing a default.
    """
    text = (anlagen_dir or "").strip()
    if not text:
        return None
    return pathlib.Path(text).expanduser()


def split_prefix(stem: str) -> tuple[int | None, str]:
    """(order number, the human half) for a file stem."""
    match = _PREFIX_RE.match(stem)
    if not match or not match.group(2):
        return None, stem
    return int(match.group(1)), match.group(2)


def _safe_member(folder: pathlib.Path, name: str) -> pathlib.Path:
    """`name` as a file directly inside `folder`, or a refusal.

    Every operation below takes a file name that travelled through a browser —
    an upload's own filename, or a row identifier echoed back by a click — and
    two of them delete or overwrite. A name is therefore never joined onto the
    folder without proving it is a plain member of it.

    `Path(name).name == name` is the whole rule: it fails for anything holding
    a separator, an absolute path or a drive. The three special names are
    enumerated because they survive it — and `..` in particular is the one a
    containment check on the joined path does NOT catch, since `folder / ".."`
    reports `folder` itself as its parent.
    """
    if (not name or name in (".", "..") or "\x00" in name
            or name != pathlib.Path(name).name):
        raise AnlagenError(f"Ungültiger Dateiname: {name!r}")
    return folder / name


def listing(folder: pathlib.Path | None) -> list[Entry]:
    """The folder in merge order — the same order `collect_anlagen` produces.

    Sorted by file name rather than by the parsed number, because the file
    name is what actually decides the order of the merged PDF. Deriving the
    display order from the parsed prefix would let the screen show one order
    while the Mappe was assembled in another.
    """
    if folder is None:
        return []
    try:
        paths = sorted(p for p in folder.iterdir()
                       if p.suffix.lower() == ".pdf")
    except OSError:
        return []
    entries = []
    for path in paths:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        number, _rest = split_prefix(path.stem)
        entries.append(Entry(name=path.name, stem=path.stem, number=number,
                             size_bytes=size))
    return entries


def _next_number(entries: list[Entry]) -> int:
    """One past the highest number in use, so an upload lands at the end.

    The end, not the first free gap: the order is "how a recruiter leafs
    through it" — Zeugnis before certificates — and a new document has no
    claim on a position he chose. He moves it up if it belongs higher.
    """
    used = [entry.number for entry in entries if entry.number is not None]
    return (max(used) + 1) if used else 1


def _free_name(folder: pathlib.Path, number: int, stem: str) -> str:
    """`NN_stem.pdf`, disambiguated if that name is taken.

    Two uploads of the same certificate must not silently become one file:
    overwriting is how a document he still needs disappears without a message.
    """
    base = f"{number:02d}_{stem}"
    if not (folder / f"{base}.pdf").exists():
        return f"{base}.pdf"
    for n in range(2, 100):
        candidate = f"{base}_{n}.pdf"
        if not (folder / candidate).exists():
            return candidate
    raise AnlagenError("Zu viele Dateien mit diesem Namen im Ordner.")


def _validate_pdf(data: bytes, filename: str) -> None:
    """Refuse anything the merge could not use, with the reason.

    Reading the whole thing through pypdf rather than sniffing `%PDF-` is the
    point: a truncated or encrypted download passes a magic-byte check, lands
    in the folder, and turns up later as "nicht lesbar" on the stack — or, if
    the stack is not looked at, as a failed build in the middle of applying.
    """
    if not (filename or "").lower().endswith(".pdf"):
        raise AnlagenError(
            "Nur PDF-Dateien — die Mappe wird aus PDFs zusammengeheftet, "
            "alles andere würde stillschweigend fehlen.")
    if not data:
        raise AnlagenError("Die Datei ist leer.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise AnlagenError(
            f"Zu groß ({len(data) / 1024 / 1024:.1f} MB) — höchstens "
            f"{MAX_UPLOAD_BYTES // 1024 // 1024} MB pro Anlage.")
    try:
        reader = PdfReader(io.BytesIO(data))
        # Asked BEFORE the pages: reading a page of an encrypted file raises,
        # and the generic "not a readable PDF" would send him looking for a
        # corrupt scan when the file is merely protected.
        if reader.is_encrypted:
            raise AnlagenError(
                "Das PDF ist passwortgeschützt — es ließe sich nicht "
                "zusammenheften. Bitte ohne Schutz speichern.")
        pages = len(reader.pages)
    except AnlagenError:
        raise
    except Exception as exc:  # pypdf raises its own family on a torn file
        raise AnlagenError(f"Kein lesbares PDF: {exc}") from exc
    if pages < 1:
        raise AnlagenError("Das PDF hat keine Seiten.")


def store(folder: pathlib.Path, filename: str, data: bytes) -> pathlib.Path:
    """Put an uploaded PDF into the folder and answer with where it landed.

    Written to a staging name and moved into place, so a build running while
    the browser is still uploading never merges half a file.
    """
    _validate_pdf(data, filename)
    folder.mkdir(parents=True, exist_ok=True)
    raw = pathlib.Path(filename).name
    _safe_member(folder, raw)  # proves it is a plain member, never a path
    # An uploaded "01_Zeugnis.pdf" must not become "07_01_Zeugnis": the number
    # the app assigns is the only one that decides position.
    _number, human = split_prefix(pathlib.Path(raw).stem)
    clean = pdf.safe_filename(human) or "Anlage"
    target = folder / _free_name(folder, _next_number(listing(folder)), clean)
    staging = folder / (target.name + _STAGE_SUFFIX)
    try:
        # Exclusive: two uploads landing on one staging name is the one way
        # this could silently merge two different documents into one file.
        with open(staging, "xb") as handle:
            handle.write(data)
    except FileExistsError as exc:
        raise AnlagenError(
            "Diese Datei wird gerade schon hochgeladen.") from exc
    except OSError as exc:
        raise AnlagenError(f"Konnte nicht gespeichert werden: {exc}") from exc
    try:
        os.replace(staging, target)
    except OSError as exc:
        staging.unlink(missing_ok=True)
        raise AnlagenError(f"Konnte nicht gespeichert werden: {exc}") from exc
    log.info("anlagen: stored %s (%d bytes)", target.name, len(data))
    return target


def _apply_order(folder: pathlib.Path, order: list[Entry]) -> None:
    """Renumber `order` to 01…NN, touching only the files that must move.

    Two phases through a staging suffix. A one-phase pass would have to rename
    a file onto a name its neighbour still holds, and `os.replace` would
    silently destroy that neighbour — which here is one of his certificates.
    """
    moves = []
    for index, entry in enumerate(order, start=1):
        _number, human = split_prefix(entry.stem)
        wanted = f"{index:02d}_{human}.pdf"
        if wanted != entry.name:
            moves.append((entry.name, wanted))
    if not moves:
        return
    staged: list[tuple[pathlib.Path, str]] = []
    try:
        for current, wanted in moves:
            stage = folder / (wanted + _STAGE_SUFFIX)
            os.replace(folder / current, stage)
            staged.append((stage, wanted))
        for stage, wanted in staged:
            os.replace(stage, folder / wanted)
    except OSError as exc:
        # Put back whatever is still parked under a staging name, so a failed
        # reorder cannot end with documents missing from the Mappe.
        for stage, wanted in staged:
            if stage.exists():
                original = next(c for c, w in moves if w == wanted)
                try:
                    os.replace(stage, folder / original)
                except OSError:
                    log.error("anlagen: %s stuck at %s", original, stage)
        raise AnlagenError(f"Umsortieren fehlgeschlagen: {exc}") from exc


def move(folder: pathlib.Path, name: str, delta: int) -> None:
    """Move one Anlage `delta` places through the merge order.

    Renaming is the mechanism because the file name IS the order — the merge
    sorts by it and he sets it from his file manager. A second place that
    decided the order would be a second thing to keep in step, and the one
    that lost would be the one an employer receives.
    """
    _safe_member(folder, name)
    entries = listing(folder)
    index = next((i for i, e in enumerate(entries) if e.name == name), None)
    if index is None:
        raise AnlagenError(f"„{name}“ liegt nicht mehr im Ordner.")
    target = index + delta
    if not 0 <= target < len(entries):
        return
    entries.insert(target, entries.pop(index))
    _apply_order(folder, entries)


def remove(folder: pathlib.Path, name: str) -> pathlib.Path:
    """Take an Anlage out of the merge order, keeping the file.

    Moved rather than unlinked: these are the only copies of scanned
    certificates some people have, and the app has no business being the
    thing that loses one. The pile is outside his folder, so it can never be
    picked up by a build or offered back by the file picker.
    """
    source = _safe_member(folder, name)
    if not source.is_file():
        raise AnlagenError(f"„{name}“ liegt nicht mehr im Ordner.")
    pile = trash_dir()
    pile.mkdir(parents=True, exist_ok=True)
    target = pile / name
    if target.exists():
        stem, suffix = target.stem, target.suffix
        for n in range(2, 1000):
            candidate = pile / f"{stem} ({n}){suffix}"
            if not candidate.exists():
                target = candidate
                break
        else:
            raise AnlagenError("Der Ablage-Ordner ist voll von diesem Namen.")
    shutil.move(str(source), str(target))
    log.info("anlagen: removed %s -> %s", name, target)
    return target
