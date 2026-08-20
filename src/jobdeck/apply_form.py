"""What a German application form asks for, assembled per posting.

Portals and ATS forms are never automated — that is a settled product decision.
The reason is the platforms' own terms plus the fact that a form submitted
without being read is irreversible, and one application per company means a
silent mistake burns that company for good. It is NOT Art. 22 GDPR, which this
docstring used to cite: that article governs the EMPLOYER making an automated
decision about the applicant, and says nothing about how the applicant fills a
form. A wrong justification is how a rule survives without ever being examined.
237 of his postings route to a form. So the work
that remains is the five minutes of typing, and every one of those minutes goes
into fields he has already written down somewhere: his address, his links, his
availability, the Stellenbezeichnung, the Referenznummer, the Anschreiben.

This module answers "what will the form ask, and what is the answer" as pure
data — no I/O, no UI. The cockpit page renders it as copy-one-click rows beside
the employer's own tab; a field with no answer yet says where to put one instead
of rendering an empty box, because a blank in a Bewerbung is worse than a gap he
can see.
"""

from dataclasses import dataclass

from jobdeck.ai.drafting import clean_title, resolve_refnr

# Applicant facts, held as settings because they are personal data: they live in
# his database, never in this repository. ONE source of truth for the keys AND
# the words — the Settings card renders these labels and the cockpit rows reuse
# them, so adding a field is one edit.
APPLICANT_LABELS = {
    "applicant_name": "Name (Vor- und Nachname)",
    "applicant_email": "E-Mail",
    "applicant_phone": "Telefon",
    "applicant_strasse": "Straße, Hausnummer",
    "applicant_plz_ort": "PLZ, Ort",
    "applicant_linkedin": "LinkedIn",
    "applicant_github": "GitHub",
    "applicant_website": "Website",
    "applicant_availability": "Verfügbar ab",
    "applicant_salary": "Gehaltsvorstellung",
}
APPLICANT_SETTINGS = tuple(APPLICANT_LABELS)


@dataclass(frozen=True)
class Field:
    """One row of the cockpit: what the form calls it, and the answer.

    `value` empty means unanswered, and `hint` then says how to answer it —
    either a setting to fill in or a step to take on this posting."""

    label: str
    value: str
    hint: str = ""
    multiline: bool = False

    @property
    def ready(self) -> bool:
        return bool(self.value)


_SETTINGS_HINT = "Settings → Bewerbungsdaten"


def _split_name(full: str) -> tuple[str, str]:
    """('Andrei', 'Sili') — forms ask for the two halves separately far more
    often than for the whole name. The LAST word is the surname and everything
    before it the given names, which is the German convention and what he wrote
    into the setting; a single word answers as a surname-less given name."""
    parts = (full or "").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def personal_fields(settings: dict) -> list[Field]:
    """The applicant half: identical for every posting, wrong nowhere."""
    get = lambda key: (settings.get(key) or "").strip()  # noqa: E731
    first, last = _split_name(get("applicant_name"))
    # the name is the one setting a form asks for in two boxes
    rows = [Field("Vorname", first, _SETTINGS_HINT),
            Field("Nachname", last, _SETTINGS_HINT)]
    rows += [Field(label, get(key), _SETTINGS_HINT)
             for key, label in APPLICANT_LABELS.items() if key != "applicant_name"]
    return rows


# Draft states whose text is an ANSWER. A discarded draft was rejected and a
# failed one was never finished, so offering either as the Anschreiben would put
# words he threw away into someone's form. A 'filed' letter is the one that
# actually went into a form, so it is the most answerable of them all.
USABLE_DRAFT_STATUS = ("ready", "approved", "sent", "filed")


def usable(draft: dict | None) -> dict | None:
    """The draft if its text may be offered as an answer, else None."""
    if draft and str(draft.get("status") or "") in USABLE_DRAFT_STATUS:
        return draft
    return None


# What the board is called when a form asks "Wie haben Sie von uns erfahren?" —
# a question German application forms ask constantly, and the adapter's internal
# name is not an answer.
SOURCE_LABELS = {
    "arbeitsagentur": "Bundesagentur für Arbeit (arbeitsagentur.de)",
    "jooble": "Jooble",
    "arbeitnow": "Arbeitnow",
}


class _row:
    """`resolve_refnr` indexes its argument like a sqlite3.Row; the cockpit holds
    plain dicts, and a missing key must read as absent rather than raise."""

    def __init__(self, data: dict):
        self._data = data or {}

    def __getitem__(self, key):
        value = self._data.get(key)
        return "" if value is None else str(value)


def posting_fields(job: dict, draft: dict | None) -> list[Field]:
    """The posting half: what THIS employer's form needs about THIS role.

    `draft` is whatever the caller judged usable — `fields()` passes it through
    `usable()` so a discarded or failed draft answers nothing.

    The Stellenbezeichnung is cleaned deterministically from the stored title
    rather than parsed back out of the draft's Betreff — HR matches on the exact
    role name, and re-parsing a sentence we built is a way to lose it. The
    Referenznummer is passed through untouched for the same reason: an id is
    either exact or wrong."""
    get = lambda key: str(job.get(key) or "").strip()  # noqa: E731
    drafted = lambda key: str((draft or {}).get(key) or "").strip()  # noqa: E731
    source = get("source")
    return [
        Field("Stellenbezeichnung", clean_title(get("title")),
              "the posting has no title stored"),
        # through the app's own resolver: an Arbeitsagentur external_id IS the
        # Refnr, and the column is empty on 186 of his 209 postings from that
        # source — reading it raw would say "none stated" while the Betreff row
        # two lines down prints it
        Field("Referenznummer", resolve_refnr(_row(job)),
              "this posting states no Referenznummer"),
        Field("Ansprechpartner", get("ansprechpartner"),
              "none named in the posting"),
        Field("Gefunden über", SOURCE_LABELS.get(source, source),
              "unknown source"),
        Field("Betreff", drafted("betreff"),
              "draft the application first — the Betreff comes with it"),
        Field("Anschreiben", drafted("anschreiben_body"),
              "draft the application first", multiline=True),
        Field("Bewerbungsmappe (PDF)", drafted("pdf_path"),
              "create the Mappe in the Postausgang"),
    ]


def fields(job: dict, draft: dict | None, settings: dict) -> list[Field]:
    """Every row the cockpit shows, applicant first: the personal block is the
    part he types into every form, so it belongs where his hand starts."""
    return personal_fields(settings) + posting_fields(job, usable(draft))


def missing(rows: list[Field]) -> list[Field]:
    """The rows with no answer yet — what the cockpit warns about up front, so a
    gap is found before he is halfway through someone else's form."""
    return [row for row in rows if not row.ready]
