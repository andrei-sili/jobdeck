"""What a Bewerbermanagementsystem's parser will make of a PDF, measured.

The research behind this (2026-09-04) put the three gates in order: a form's
knock-out questions reject automatically, the parser only RANKS, and a
person decides in about forty seconds. This module serves the second gate.
It does not guess at a vendor's algorithm — nobody publishes one — it checks
the properties every parser depends on and that the user's own documents
were found to break: text that extracts at all, fonts embedded as real
TrueType rather than Type 3 outlines (a variable webfont printed through
Chrome comes out as Type 3, and some extractors then lose the spaces),
section headings that come out as words rather than letter-spaced
characters, and contact details that are text and not an icon.

Everything here reads a finished PDF with pypdf, the same library the build
writes with, so a check can never disagree with the file it describes.
"""

import dataclasses
import pathlib
import re

from pypdf import PdfReader

# The headings a German CV parser keys on, in the spelling a section label
# usually carries. Case-insensitive, and matched after letter-spacing has been
# folded back, so "B E R U F S E R F A H R U N G" still counts — as a
# finding, not as a heading.
HEADINGS = ("Berufserfahrung", "Ausbildung", "Kenntnisse", "Projekte",
            "Sprachen", "Zertifikate", "Profil")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"\+?\d[\d\s()/-]{7,}\d")  # \s: a number may wrap
# A run of single letters separated by single spaces: what a letter-spaced
# heading becomes under pdfminer-style extraction.
_SPACED_RE = re.compile(r"\b(?:[A-ZÄÖÜ] ){4,}[A-ZÄÖÜ]\b")
# A "word" no German CV contains: spaces lost between words by the extractor.
# Letters only — a URL or an e-mail address is legitimately that long, and the
# first live run flagged github.com/…/ecommerce-microservices as glued text.
_GLUED_RE = re.compile(r"[A-Za-zÄÖÜäöüß]{40,}")


@dataclasses.dataclass(frozen=True)
class Check:
    """One property, with the sentence the screen prints for it."""

    ok: bool
    text: str


@dataclasses.dataclass(frozen=True)
class Report:
    path: str
    pages: int
    size_bytes: int
    text_chars: int
    type3_fonts: int
    fonts: int
    headings: tuple[str, ...]
    checks: tuple[Check, ...]
    error: str = ""

    @property
    def passed(self) -> bool:
        return not self.error and all(c.ok for c in self.checks)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c.ok)


def _fonts(reader: PdfReader) -> tuple[int, int]:
    """(fonts, of which Type 3) across every page, by resource dictionary."""
    seen: set = set()
    type3 = 0
    for page in reader.pages:
        resources = page.get("/Resources") or {}
        fonts = resources.get("/Font") or {}
        for name, ref in fonts.items():
            try:
                font = ref.get_object()
            except Exception:  # noqa: BLE001 — a broken font is a finding, not a crash
                continue
            key = (id(font), str(name))
            if key in seen:
                continue
            seen.add(key)
            if str(font.get("/Subtype", "")) == "/Type3":
                type3 += 1
    return len(seen), type3


def _unspace(text: str) -> str:
    """Fold letter-spaced runs back into words for the heading search."""
    return _SPACED_RE.sub(lambda m: m.group(0).replace(" ", ""), text)


def inspect(path: pathlib.Path, *, budget_bytes: int = 0,
            expect_headings: bool = True) -> Report:
    """Measure one PDF the way a parser meets it.

    `expect_headings` is False for a document that is not a CV — a letter
    or the merged Anlagen carry no section headings and must not be marked
    down for it."""
    path = pathlib.Path(path)
    if not path.is_file():
        return Report(str(path), 0, 0, 0, 0, 0, (), (),
                      error="Datei nicht gefunden")
    try:
        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        fonts, type3 = _fonts(reader)
        pages = len(reader.pages)
    except Exception as exc:  # noqa: BLE001 — pypdf raises many kinds on a torn file
        return Report(str(path), 0, path.stat().st_size, 0, 0, 0, (), (),
                      error=f"PDF nicht lesbar: {exc}")
    size = path.stat().st_size
    folded = _unspace(text)
    lowered = folded.lower()
    headings = tuple(h for h in HEADINGS if h.lower() in lowered)
    spaced = _SPACED_RE.search(text) is not None
    glued = _GLUED_RE.search(text) is not None

    checks = [
        Check(len(text.strip()) >= 200,
              "Text ist extrahierbar" if len(text.strip()) >= 200
              else "Kaum Text extrahierbar — ein Scan ohne Textebene?"),
        Check(type3 == 0,
              "Schriften als TrueType eingebettet"
              if type3 == 0 else
              f"{type3} Schrift(en) als Type 3 eingebettet — variable Webfonts "
              f"durch statische ersetzen, sonst verlieren Parser die Leerzeichen"),
        Check(not glued,
              "Wörter bleiben getrennt" if not glued
              else "Wörter kleben zusammen — der Parser findet keine Begriffe"),
        Check(not spaced,
              "Überschriften kommen als Wörter an" if not spaced
              else "Eine Überschrift kommt Buchstabe für Buchstabe an — "
                   "letter-spacing auf Sektions-Titeln entfernen"),
        Check(_EMAIL_RE.search(text) is not None,
              "E-Mail-Adresse steht im Text"
              if _EMAIL_RE.search(text) else
              "Keine E-Mail-Adresse im Text — steht sie in einer Grafik?"),
        Check(_PHONE_RE.search(text) is not None,
              "Telefonnummer steht im Text"
              if _PHONE_RE.search(text) else "Keine Telefonnummer im Text"),
    ]
    if expect_headings:
        core = {"Berufserfahrung", "Ausbildung", "Kenntnisse"}
        found = core & set(headings)
        checks.append(Check(
            len(found) == len(core),
            "Standard-Überschriften gefunden: " + ", ".join(headings)
            if len(found) == len(core) else
            "Standard-Überschriften fehlen: " + ", ".join(sorted(core - found))))
    if budget_bytes:
        checks.append(Check(
            size <= budget_bytes,
            f"{size / 1024 / 1024:.1f} MB — unter dem Portal-Budget"
            if size <= budget_bytes else
            f"{size / 1024 / 1024:.1f} MB — über dem Portal-Budget von "
            f"{budget_bytes / 1024 / 1024:.1f} MB"))
    return Report(str(path), pages, size, len(text), type3, fonts, headings,
                  tuple(checks))
