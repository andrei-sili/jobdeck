"""Job inbox: discovered postings with per-job actions."""

import logging
import math
import pathlib
from dataclasses import dataclass

from fastapi.responses import RedirectResponse
from nicegui import app, run, ui

from jobdeck import apply_channel, db, freshness
from jobdeck.ai import scoring
from jobdeck.ai.drafting import clean_title
from jobdeck.dedupe import duplicates_for_jobs
from jobdeck.services import apply_resolve, contact_lookup, drafting, liveness, mappe
from jobdeck.ui import helpers, live
from jobdeck.ui.helpers import (
    open_in_system,
    openable_url,
    posting_markdown,
)
from jobdeck.ui.layout import frame
from jobdeck.ui.rail import STELLEN_PATH

log = logging.getLogger(__name__)

# One page of postings. It used to be a hard limit of 100 with no way past it,
# which left 187 of his 287 open postings unreachable in the UI entirely — and
# no number on screen said so.
PAGE_SIZE = 50

# How much of an advert is rendered. Markdown of a whole posting is cheap; the
# reason for a bound at all is that a scraped description is untrusted and can
# be arbitrarily long. Well above the longest thing his corpus holds (his
# longest is ~28k characters), and where it does bite, the reader says so
# instead of simply stopping mid-sentence.
DESCRIPTION_LIMIT = 40_000

# ONE list of named views, replacing six status filters, four pile switches and
# a grouping toggle. Those controls could be combined into states that describe
# nothing ("applied postings, mismatches only"), and the two pile switches had
# to be mutually exclusive — which is what made them echo each other into an
# endless refresh loop. A single value cannot disagree with itself, and a view
# he can name is a view he can find his way back to.
#
# Everything a view does is stated here as data: which status it stands on and
# what it does with each pile. A screen that computed its filters in the handler
# is a screen whose printed total can disagree with its own rows.
_WORKING = {"mismatches": "exclude", "gone": "exclude", "applied": "exclude",
            "old": "exclude"}
_EVERYTHING = {"mismatches": "include", "gone": "include", "applied": "include",
               "old": "include"}


@dataclass(frozen=True)
class View:
    """One named way of looking at the corpus."""

    key: str
    label: str
    status: str | None   # None: whatever he did with it, this view still holds it
    filters: dict
    empty: str


VIEWS = (
    View("neu", "Neu", "new", {**_WORKING, "opened": "exclude"},
         "Nichts Neues — du hast alles gesehen, was in der Arbeitsliste steht."),
    View("offen", "Alle offen", "new", _WORKING,
         "Die Arbeitsliste ist leer. Ein Suchprofil füllt sie wieder."),
    View("vorgemerkt", "Vorgemerkt", None, {**_EVERYTHING, "bookmarked": "only"},
         "Nichts vorgemerkt. Mit „s“ legst du eine Anzeige hier ab."),
    View("in_arbeit", "In Arbeit", None, {**_EVERYTHING, "in_progress": "only"},
         "Keine Bewerbung in Arbeit."),
    View("beworben", "Beworben", "applied", _EVERYTHING,
         "Noch keine Bewerbung aus einer Anzeige heraus."),
    View("kein_interesse", "Kein Interesse", "skipped", _EVERYTHING,
         "Du hast noch keine Anzeige weggelegt."),
    View("passt_nicht", "Passt nicht", "new", {**_EVERYTHING, "mismatches": "only"},
         "Keine Anzeige verletzt eine harte Anforderung."),
    View("alt", "Alt", "new", {**_EVERYTHING, "old": "only"},
         "Keine alten Anzeigen — alles in der Arbeitsliste ist frisch."),
    View("offline", "Offline", "new", {**_EVERYTHING, "gone": "only"},
         "Keine tote Anzeige — alles Geprüfte steht noch online."),
    View("firma_kontaktiert", "Firma schon kontaktiert", "new",
         {**_EVERYTHING, "applied": "only"},
         "Keine Stelle bei einer Firma, bei der du dich schon beworben hast."),
    View("doppelt", "Doppelt", "duplicate", _EVERYTHING,
         "Keine Anzeige wurde als Doppelte einer Bewerbung erkannt."),
)
DEFAULT_VIEW = VIEWS[0].key
_BY_KEY = {view.key: view for view in VIEWS}


def view_for(key: str) -> View:
    """The named view, falling back to the default rather than raising.

    The key reaches here from a control and one day from a URL; an unknown one
    is a screen he cannot open, and the default is always a safe answer."""
    return _BY_KEY.get(key, _BY_KEY[DEFAULT_VIEW])


def _hidden_line(view: View, counts: dict, stale_age_days: int,
                 search: str = "") -> str:
    """What this view is not showing, derived from the filters it actually used
    so the label cannot contradict the list. Independent statements, never a
    total: a posting can be a mismatch AND offline AND old.

    A search narrows the list without hiding a pile, so it is stated first and
    in its own words — otherwise the pile counts read as if they explained a
    three-row result."""
    parts = []
    if search.strip():
        parts.append(f"gefiltert nach „{search.strip()}“")
    if view.filters.get("opened") == "exclude" and counts.get("read"):
        parts.append(f"{counts['read']} schon gelesen ausgeblendet")
    for arm, count_key, hidden, only in (
        ("mismatches", "mismatches", "{n} passen nicht",
         "{n} verletzen eine harte Anforderung"),
        ("gone", "dead", "{n} offline", "{n} Anzeigen sind offline"),
        ("applied", "applied_firm", "{n} bei schon beworbenen Firmen",
         "{n} Stellen bei Firmen, bei denen du dich schon beworben hast"),
        ("old", "old", f"{{n}} älter als {stale_age_days} Tage",
         f"{{n}} Anzeigen älter als {stale_age_days} Tage"),
    ):
        count = counts.get(count_key, 0)
        if view.filters.get(arm) == "exclude" and count:
            parts.append(hidden.format(n=count) + " ausgeblendet")
        elif view.filters.get(arm) == "only":
            parts.append(only.format(n=count))
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# One primary action per posting, and its whole state machine.
#
# 650 of his 803 postings sit in the FIRST state — the channel has never been
# resolved — so a screen offering "apply" everywhere would be wrong on four
# rows out of five. A blocked action states the reason NEXT TO ITSELF rather
# than in a tooltip: he reads this list with the keyboard, and a tooltip is a
# thing only a mouse can find.
# ---------------------------------------------------------------------------
ACTION_NONE = "none"
ACTION_RESOLVE = "resolve"
ACTION_DRAFT = "draft"
ACTION_FORM = "form"
ACTION_QUEUE = "queue"
ACTION_RECORD = "record"
ACTION_REVIVE = "revive"
ACTION_OPEN = "open"


@dataclass(frozen=True)
class Action:
    """What pressing the main button does here — and if it cannot, why not."""

    key: str
    label: str
    reason: str = ""
    enabled: bool = True


_FORM_CHANNELS = (apply_channel.CHANNEL_ATS, apply_channel.CHANNEL_BOARD,
                  apply_channel.CHANNEL_COMPANY_SITE)


def primary_action(job: dict, already: dict | None = None) -> Action:
    """The one thing to do with this posting next.

    The order is the order of the pipeline, and the refusals come first: a
    posting at a company he has already written to can never become an
    application, so nothing further down may offer to start one."""
    status = str(job.get("status") or "")
    draft_status = str(job.get("draft_status") or "")
    if status == "skipped":
        # He put it away himself, and nothing else could put it back: the
        # "Kein Interesse" view had no exit at all, while the channel arms
        # below still cheerfully offered to write an application for it.
        return Action(ACTION_REVIVE, "Zurück in die Arbeitsliste",
                      "Du hattest sie weggelegt.")
    if status == "applied":
        return Action(ACTION_NONE, "Beworben", "Diese Anzeige ist eingetragen.",
                      enabled=False)
    if already:
        return Action(ACTION_NONE, "Bewerbung nicht möglich",
                      helpers.applied_line(already), enabled=False)
    if status == "duplicate":
        return Action(ACTION_NONE, "Bewerbung nicht möglich",
                      "Diese Anzeige gehört zu einer Firma, bei der schon eine "
                      "Bewerbung liegt.", enabled=False)
    if draft_status == "generating" and not drafting.claim_is_stale(
            job.get("draft_updated_at")):
        return Action(ACTION_NONE, "Wird geschrieben …",
                      "Das dauert etwa eine Minute.", enabled=False)
    if draft_status in ("ready", "approved"):
        return Action(ACTION_QUEUE, "Prüfen und senden")
    if draft_status == "sending":
        return Action(ACTION_QUEUE, "Versand auflösen",
                      "Ein Versand läuft — oder er ist stecken geblieben.")
    if draft_status == "failed" or draft_status == "generating":
        return Action(ACTION_DRAFT, "Erneut schreiben",
                      "Der letzte Versuch ist nicht fertig geworden.")
    if status == "portal":
        return Action(ACTION_RECORD, "Als beworben eintragen",
                      "Das Formular war offen — trag die Bewerbung ein, wenn "
                      "du sie abgeschickt hast.")
    channel = str(job.get("apply_channel") or "")
    if not channel:
        return Action(ACTION_RESOLVE, "Kanal ermitteln",
                      "Noch unbekannt, wie man sich hier bewirbt.")
    if channel == apply_channel.CHANNEL_DIRECT_EMAIL:
        return Action(ACTION_DRAFT, "Bewerbung per E-Mail erstellen")
    if channel in _FORM_CHANNELS:
        return Action(ACTION_FORM, "Formular ausfüllen")
    return Action(ACTION_OPEN, "Anzeige öffnen und selbst prüfen",
                  "Kein Bewerbungsweg gefunden — die Anzeige nennt keinen.")


def _score_line(job: dict) -> str:
    """The score, and what its age did to it: ' · match 92 → 72 · 61 Tage alt'.

    Both numbers come from the row the query returned, so the one shown is the
    one that decided the position (see freshness.py). Showing the arrow only
    when age actually cost something keeps a fresh posting's line quiet."""
    score = job["match_score"]
    if score is None:
        return ""
    age = job["age_days"]
    effective = job["effective_score"]
    if age is None:
        aged = " · Datum unbekannt"
    elif age <= 0:
        # today, or a date in the future: the boards state no timezone, so a
        # posting can legitimately read as -1 days. "vor -1 Tagen" is nonsense.
        aged = " · heute"
    else:
        aged = f" · {age} {'Tag' if age == 1 else 'Tage'} alt"
    if effective != score:
        return f" · match {score} → {effective}{aged}"
    return f" · match {score}{aged}"


_CHANNEL_SHORT = {
    apply_channel.CHANNEL_DIRECT_EMAIL: "E-Mail",
    apply_channel.CHANNEL_ATS: "Formular",
    apply_channel.CHANNEL_BOARD: "Formular",
    apply_channel.CHANNEL_COMPANY_SITE: "Formular",
    apply_channel.CHANNEL_UNKNOWN: "kein Weg",
}


def _age_short(age: object) -> str:
    """'heute', '1 T', '34 T', or a stated absence — never a silent blank."""
    if age is None:
        return "Datum ?"
    days = int(age)
    return "heute" if days <= 0 else f"{days} T"


def row_meta(job: dict) -> str:
    """'34 T · Formular · 45–55 T€ · BA' — one line of facts under a row.

    Every part is a fact the posting really states. Nothing is guessed and
    nothing is silently left out: an unresolved channel says so ("Kanal offen")
    rather than looking like a form, because which of the two it turns out to
    be decides whether an application here is one press or twenty minutes."""
    parts = [_age_short(job.get("age_days"))]
    channel = str(job.get("apply_channel") or "")
    parts.append(_CHANNEL_SHORT.get(channel, "Kanal offen") if channel
                 else "Kanal offen")
    salary = _salary_short(job)
    if salary:
        parts.append(salary)
    source = str(job.get("source") or "")
    if source:
        parts.append(_SOURCE_LABELS.get(source, source))
    return " · ".join(parts)


_SOURCE_LABELS = {"arbeitsagentur": "BA", "arbeitnow": "Arbeitnow",
                  "jooble": "Jooble"}


def _salary_short(job: dict) -> str:
    """'45–55 T€' for a yearly range, '' for anything else.

    Only the yearly shape is abbreviated: an hourly wage rounded to thousands
    would be a different offer, and the reader states every figure in full."""
    if str(job.get("salary_period") or "").strip().upper() != "JAHRESGEHALT":
        return ""
    figures = []
    for key in ("salary_from", "salary_to"):
        try:
            value = float(str(job.get(key) or "").strip())
        except (TypeError, ValueError):
            return ""
        if not math.isfinite(value) or value < 1000:
            return ""
        figures.append(f"{round(value / 1000)}")
    return f"{figures[0]}–{figures[1]} T€" if len(figures) == 2 else ""


def _load_jobs(view_key: str, page: int, search: str = "",
               keep_ids: tuple[int, ...] = ()) -> dict:
    """One page of a named view, with everything needed to describe it.

    A row stands for a COMPANY — its best-ranked posting, with the others
    listed beneath it. Only one application per company is possible anyway, so
    the count and the page both count companies and the printed range stays
    true to what is on screen. It is no longer a toggle: a screen that could be
    switched between two units is a screen whose numbers need explaining.

    The page number is clamped HERE, against the total this very query saw: a
    view change or a background poll can shrink the result set under the user's
    feet, and asking for page 5 of a two-page list must show the last page
    rather than an empty one."""
    view = view_for(view_key)
    with db.db() as con:
        # The signature is read FIRST, before a single row. sqlite3 runs each
        # SELECT in its own read snapshot, so a poll or a liveness pass
        # committing between the two reads would otherwise marry STALE rows to
        # a FRESH signature — and the watcher would then record that signature
        # as "what the page is showing" and never rebuild. This order can only
        # fail the safe way: one rebuild nobody needed.
        signature = db.data_signature(con)
        stale_age_days = freshness.stale_age_setting(
            db.get_setting(con, "stale_age_days", ""))
        filters = {**view.filters, "stale_age_days": stale_age_days,
                   "search": search, "keep_ids": keep_ids}
        total = db.count_job_groups(con, view.status, **filters)
        read_total = None
        if view.filters.get("opened") == "exclude":
            read_total = db.count_job_groups(
                con, view.status, **{**filters, "opened": "include"})
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        rows = [dict(r) for r in db.list_job_groups(
            con, view.status, limit=PAGE_SIZE, offset=page * PAGE_SIZE, **filters)]
        keys = [r["company_key"] for r in rows if r["company_count"] > 1]
        siblings: dict[str, list[dict]] = {}
        for row in db.list_company_siblings(con, keys, view.status, **filters):
            siblings.setdefault(row["company_key"], []).append(dict(row))
        # Asked of the duplicate gate itself, for the rows on screen — see
        # dedupe.duplicates_for_jobs for why the stored flag cannot answer it.
        on_screen = rows + [r for group in siblings.values() for r in group]
        return {
            "signature": signature,
            "view": view,
            "applied": duplicates_for_jobs(con, on_screen),
            "rows": rows,
            "siblings": siblings,
            "counts": {
                "mismatches": db.count_mismatches(con, view.status),
                "dead": db.count_gone_jobs(con, view.status),
                "applied_firm": db.count_applied_firm_jobs(con, view.status),
                "old": db.count_old_jobs(con, view.status, stale_age_days),
                # What the landing view's own filter hides, counted in the same
                # unit as the list it labels: the difference between this view
                # and the same view with the read postings put back.
                "read": (read_total - total) if read_total is not None else 0,
            },
            "search": search,
            "stale_age_days": stale_age_days,
            "total": total,
            "page": page,
            "pages": pages,
        }


def _range_line(page: int, total: int, shown: int) -> str:
    """'51–100 von 266 Firmen' — where in the pipeline this page sits.

    The unit is named because a row is a COMPANY while the pile counts beside
    it are POSTINGS, and an unlabelled pair of numbers invites comparing them."""
    if not total:
        return ""
    first = page * PAGE_SIZE + 1
    return f"{first}–{first + shown - 1} von {total} Firmen"


def _signature() -> tuple:
    """One cheap read of everything this page's rows can say (see ui/live.py)."""
    with db.db() as con:
        return db.data_signature(con)


def _set_status(job_id: int, status: str):
    with db.db() as con:
        db.set_job_status(con, job_id, status)


def _confirm_applied(job_id: int, kanal: str):
    with db.db() as con:
        return db.apply_job(con, job_id, kanal=kanal)


def _load_draft(job_id: int):
    with db.db() as con:
        row = db.get_draft_by_job(con, job_id)
        return dict(row) if row is not None else None


def _mark_opened(job_id: int) -> str:
    """Record that he read it, and answer with the stamp that was written.

    The page holds the row it is showing; giving it the real value keeps that
    copy and the database saying the same thing without a second query — and a
    German word standing in for a timestamp would be a lie the row's own
    fingerprint would carry."""
    with db.db() as con:
        db.mark_job_opened(con, job_id)
        row = db.get_job(con, job_id)
        return "" if row is None else str(row["opened_at"] or "")


def _set_bookmark(job_id: int, marked: bool):
    with db.db() as con:
        db.set_bookmark(con, job_id, marked)


def _set_contact_email(job_id: int, email: str):
    with db.db() as con:
        db.set_contact_email(con, job_id, email, "web_lookup")


def _apply_line(job: dict) -> str:
    """Human one-liner for the resolved apply channel; '' when not yet resolved."""
    channel, vendor = job["apply_channel"] or "", job["ats_vendor"] or ""
    if channel == apply_channel.CHANNEL_DIRECT_EMAIL:
        return "Bewerbung: direkt per E-Mail"
    if channel in (apply_channel.CHANNEL_ATS, apply_channel.CHANNEL_BOARD):
        return f"Bewerbung über {vendor}" if vendor else "Bewerbung über ein Portal"
    if channel == apply_channel.CHANNEL_COMPANY_SITE:
        return "Bewerbung: Formular auf der Firmen-Website"
    if channel == apply_channel.CHANNEL_UNKNOWN:
        # Resolved, and the answer was "no way". Saying "noch nicht ermittelt"
        # here made one screen describe the same posting three ways, one of
        # them false.
        return "Kein Bewerbungsweg gefunden"
    return ""


# What the posting's own draft is doing, said on the row itself. Writing one
# takes about a minute, and for that minute the only feedback was a toast that
# had already faded — so a second press answered "already being generated" and
# the app looked broken. 'discarded' says nothing on purpose: the posting is
# back to where it started, and a line about a draft that no longer exists
# would only be in the way.
_DRAFT_LINES = {
    "ready": ("✓ Entwurf fertig — in der Review queue prüfen und senden.",
              "text-sm text-green-700"),
    "approved": ("✓ Entwurf freigegeben — wartet in der Review queue auf den "
                 "Versand.", "text-sm text-green-700"),
    "sending": ("Ein Versand läuft — oder er ist stecken geblieben. In der "
                "Review queue auflösen.", "text-sm text-amber-700"),
    "failed": ("⚠ Der Entwurf ist fehlgeschlagen — neu schreiben, oder in der "
               "Review queue verwerfen.", "text-sm text-red-700"),
    "sent": ("✓ Bewerbung gesendet.", "text-sm text-gray-600"),
}


# A claim whose process died says so instead of promising a minute forever —
# and it must say the SAME thing the review queue says about the same row, so
# both ask services/drafting. This is also the line that has to stay honest
# about the Draft button below it: the button is what restarts an abandoned
# draft, so it comes back exactly when this text starts calling it abandoned.
_CLAIM_LIVE = ("✍ Die Bewerbung wird gerade geschrieben — das dauert etwa "
               "eine Minute.", "text-sm text-blue-700")
_CLAIM_ABANDONED = ("⚠ Der Entwurf wurde begonnen und nie fertig — der "
                    "Vorgang ist abgebrochen. Erneut auf „Draft "
                    "application“ drücken.", "text-sm text-amber-700")


def _draft_line(draft_status: object, draft_updated_at: object = None
                ) -> tuple[str, str]:
    """(text, CSS classes) for the posting's draft state; ('', '') for none."""
    if str(draft_status or "") == "generating":
        return (_CLAIM_ABANDONED if drafting.claim_is_stale(draft_updated_at)
                else _CLAIM_LIVE)
    return _DRAFT_LINES.get(str(draft_status or ""), ("", ""))


# What the board's period code means, in the language the row is written in.
# An unknown code prints NOTHING rather than shouting an enum at him: the
# figures still stand on their own, and the vocabulary is somebody else's.
_SALARY_PERIODS = {
    "JAHRESGEHALT": "Jahresgehalt",
    "MONATSGEHALT": "Monatsgehalt",
    "WOCHENGEHALT": "Wochengehalt",
    "STUNDENLOHN": "Stundenlohn",
}


def _euro(raw: str) -> str:
    """A stored figure in German notation: 55000 → '55.000', 30.32 → '30,32'.

    Cents survive because they have to: the same field carries a yearly salary
    and an hourly wage, and 30.32 €/h printed as '30' is a different offer.

    TOTAL by construction — it renders a value read from the database, and a row
    that cannot be rendered takes the whole inbox down with it. `int(inf)` and
    `int(nan)` raise, so the conversion happens inside the guard."""
    try:
        value = float(raw)
        if not math.isfinite(value):
            return ""
        whole = f"{int(value):,}".replace(",", ".")
    except (TypeError, ValueError, OverflowError):
        return ""
    cents = round(value % 1, 2)
    return whole if not cents else f"{whole},{f'{cents:.2f}'[2:]}"


def _salary_line(job: dict) -> str:
    """'Gehalt: 55.000 – 65.000 € (Jahresgehalt)' — '' when none was stated.

    The Arbeitsagentur states a pay range on a minority of postings (10 of 40
    probed live) and the app threw it away; where it exists it is the one fact
    he otherwise has to open the ad to learn."""
    figures = [_euro(str(job[key] or "").strip())
               for key in ("salary_from", "salary_to")]
    span = " – ".join(figure for figure in figures if figure)
    if not span:
        return ""
    period = _SALARY_PERIODS.get(str(job["salary_period"] or "").strip().upper(), "")
    return f"Gehalt: {span} €" + (f" ({period})" if period else "")


def reader_notes(job: dict, already: dict | None) -> list[tuple[str, str]]:
    """The warnings that belong ABOVE the advert text, worst first.

    Each is a fact the posting itself carries, and each changes whether reading
    on is worth his time — so none of them may wait until after a wall of
    prose."""
    notes = []
    if already:
        notes.append((helpers.applied_line(already), "danger"))
    if job.get("liveness") == liveness.LIVENESS_GONE:
        checked = str(job.get("liveness_checked_at") or "")[:10]
        notes.append((f"⚠ Anzeige offline — beim letzten Abruf am {checked} "
                      f"nicht mehr vorhanden.", "danger"))
    if job.get("temp_agency"):
        # The employer is a staffing firm and the work happens at a client's
        # site — worth knowing before writing a letter, and the reason the
        # letter never borrows this posting's address.
        notes.append(("⚠ Arbeitnehmerüberlassung — der Arbeitsort gehört zu "
                      "einem Kundenbetrieb.", "warn"))
    draft_text, _ = _draft_line(job.get("draft_status"),
                                job.get("draft_updated_at"))
    if draft_text:
        notes.append((draft_text, "warn" if "⚠" in draft_text else ""))
    return notes


def reader_facts(job: dict) -> list[tuple[str, str]]:
    """The short facts block, with '—' wherever the posting states nothing.

    An empty row rather than a missing one: "Gehalt —" says the advert is
    silent about pay, and a row that simply vanished would leave him wondering
    whether the app had lost it."""
    return [
        ("Refnr", str(job.get("refnr") or "").strip() or "—"),
        ("Ort", str(job.get("location") or "").strip() or "—"),
        ("Gehalt", _salary_line(job).partition(": ")[2] or "—"),
        ("Kanal", _apply_line(job) or "noch nicht ermittelt"),
        ("Gefunden", str(job.get("published_on") or job.get("fetched_at")
                         or "")[:10] or "—"),
    ]


def _verdict_heading(job: dict) -> str:
    """'WARUM 92' — and 'WARUM 92 · durch das Alter noch 72' when age moved it.

    The heading has to name the score the REASONING is about. The model was
    asked about the posting, not about its age; heading its answer with the
    aged number produced 'WARUM 72' over a paragraph arguing for a near-perfect
    match and never mentioning how old the advert is."""
    score = job.get("match_score")
    if score is None:
        return "WARUM"
    effective = job.get("effective_score")
    if effective is not None and effective != score:
        return f"WARUM {score} · durch das Alter noch {effective}"
    return f"WARUM {score}"


def _row_fingerprint(job: dict) -> tuple:
    """Everything a row and its reader actually draw.

    The live signature is global by design — a score landing on a posting he
    is not looking at moves it — so most ticks change nothing this page shows.
    Redrawing anyway empties two scroll containers and loses his place in a
    two-page advert."""
    return tuple(job.get(key) for key in (
        "id", "company", "title", "effective_score", "match_score", "age_days",
        "match_reason", "status", "apply_channel", "ats_vendor", "apply_url",
        "url", "contact_email", "draft_status", "draft_updated_at", "liveness",
        "liveness_checked_at", "opened_at", "bookmarked_at", "temp_agency",
        "salary_from", "salary_to", "salary_period", "description", "refnr",
        "location", "published_on", "fetched_at", "source", "company_count",
        "company_key",
    ))


def _wants_a_letter(job: dict, already: dict | None) -> bool:
    """Should this posting offer to write an Anschreiben as a SECOND action?

    Only where the primary action leads to a form: an e-mail application is
    the letter, and a posting that cannot become an application at all must
    not be offered one."""
    if already or str(job.get("status") or "") not in ("new", "portal"):
        return False
    if str(job.get("draft_status") or "") in db.OPEN_DRAFT_STATUSES:
        return False
    return primary_action(job, already).key in (ACTION_FORM, ACTION_OPEN)


def _openable_url(job: dict) -> str:
    """The URL a posting's buttons may hand to the browser, '' when none is
    safe. The resolved apply link wins over the raw feed URL."""
    return openable_url(job["apply_url"] or job["url"] or "")


@app.get("/jobs")
def legacy_jobs_page():
    """The inbox's old address. Postings are the screen he opens the app for,
    so they took the home route; a bookmark or an old link still lands.

    An HTTP redirect rather than a page that draws nothing and then navigates:
    the moved address should answer as moved, and a page whose only job is to
    jump renders an empty screen first."""
    return RedirectResponse(STELLEN_PATH)


@ui.page(STELLEN_PATH)
async def jobs_page():
    async with frame("Stellen", current="stellen", padded=False):
        state = {"view": DEFAULT_VIEW, "page": 0, "search": "",
                 "selected": None, "prefer_index": None}
        # What is on screen right now, so a tick that changes nothing this page
        # shows can draw nothing at all.
        drawn: dict = {}
        # The rows currently on screen, by id — what the keyboard moves through
        # and what the reader is drawn from, so both always describe the very
        # page he is looking at rather than a query run again behind his back.
        shown: dict[int, dict] = {}
        order: list[int] = []
        row_elements: dict = {}
        siblings: dict = {}
        # What he has opened during this sitting. The "Neu" view hides what he
        # has read, so without this the posting he is reading would drop out of
        # the list on the next tick and take the reading pane with it.
        read_here: set[int] = set()
        # What the duplicate gate says about the rows currently on screen,
        # refreshed with them. Not `jobs.duplicate_of`: that is written once at
        # discovery, so every application he sends makes more rows stale.
        applied: dict[int, dict] = {}
        refresh_gen = {"n": 0}   # rapid view flips: last request wins

        # Feedback has to outlive the row that asked for it. A handler runs in
        # the slot of the element that fired it, and a refresh clears the list —
        # so a dialog or notification built afterwards has no live parent and
        # raises instead of appearing, silently swallowing the very error it was
        # meant to report. This host is a sibling of both panes.
        overlay = ui.column().classes("contents")

        def say(message: str, **kwargs) -> None:
            """Tell the user something, from a slot no refresh can delete."""
            with overlay:
                ui.notify(message, **kwargs)

        with ui.column().classes("jd-screen w-full"):
            with ui.row().classes("jd-strip"):
                ui.label("Stellen").classes("jd-strip-title")
                range_label = ui.label().classes("jd-meta")
                hidden_label = ui.label().classes("jd-meta")
                # The chip belongs at the top of the page, not beside the
                # filters: an update notice under fifty rows is an update
                # notice nobody sees.
                chip_host = ui.row().classes("items-center ml-auto gap-2")
                with chip_host:
                    ui.label("j k bewegen · x kein Interesse · s merken · "
                             "⏎ Hauptaktion").classes("jd-meta")
            with ui.element("div").classes("jd-panes"):
                with ui.element("div").classes("jd-list"):
                    with ui.row().classes("items-center gap-2 p-2 border-b"):
                        search_box = ui.input(placeholder="suchen …") \
                            .props("dense outlined clearable").classes("flex-1")
                        search_box.on_value_change(
                            lambda e: set_search(e.value or ""))
                        ui.select(
                            {view.key: view.label for view in VIEWS},
                            value=DEFAULT_VIEW,
                            on_change=lambda e: set_view(e.value),
                        ).mark("view-select").props("dense outlined borderless") \
                            .classes("min-w-36")
                    rows_host = ui.column().classes("jd-rows w-full gap-0") \
                        .props('role=listbox aria-label="Anzeigen"')
                    pager = ui.row().classes("items-center gap-2 p-2 border-t")
                reader = ui.element("div").classes("jd-reader")

        # ------------------------------------------------------------------
        # loading and rendering
        # ------------------------------------------------------------------
        async def refresh(force: bool = False) -> None:
            """Re-read and re-draw. `force` when HE did something.

            The skip below exists for the watcher's ticks, which are mostly
            about postings he is not looking at. It must never swallow his own
            action: the pressed button relabels itself to "wird geschrieben …"
            and only a re-render puts it back."""
            refresh_gen["n"] += 1
            gen = refresh_gen["n"]
            view = await run.io_bound(
                _load_jobs, state["view"], state["page"], state["search"],
                tuple(read_here))
            if gen != refresh_gen["n"] or view is None:
                return  # superseded, or the page is going away
            state["page"] = view["page"]   # the loader clamped it to what exists
            live_view.mark(view["signature"])
            fresh = {row["id"]: row for row in view["rows"]}
            new_order = [row["id"] for row in view["rows"]]
            selected, dropped = _next_selection(fresh, new_order)

            # A rebuild empties both panes, and both are scroll containers: he
            # loses his place in a two-page advert AND in the list. The
            # signature is global — a score landing on a posting he is not
            # looking at moves it — so most ticks change nothing this page
            # shows. Compare what would be drawn and draw nothing when it is
            # the same.
            page_state = (new_order, [_row_fingerprint(r) for r in view["rows"]],
                          selected, view["total"], view["page"],
                          bool(dropped), sorted(applied) != sorted(view["applied"]))
            unchanged = not force and page_state == drawn.get("state")
            drawn["state"] = page_state

            applied.clear()
            applied.update(view["applied"])
            siblings.clear()
            siblings.update(view["siblings"])
            shown.clear()
            shown.update(fresh)
            order.clear()
            order.extend(new_order)
            state["selected"] = selected
            state["prefer_index"] = None
            if dropped is not None:
                # It left the list while he was reading it — scored 0, marked
                # gone, aged past the threshold. Keeping it in the pane and
                # saying so beats swapping a different posting under his eyes.
                shown[dropped["id"]] = dropped
            if unchanged:
                return
            range_label.set_text(_range_line(
                view["page"], view["total"], len(view["rows"])))
            hidden_label.set_text(_hidden_line(
                view["view"], view["counts"], view["stale_age_days"],
                search=view["search"]))
            render_rows(view)
            render_pager(view)
            render_reader(gone=dropped is not None)

        def _next_selection(fresh: dict, new_order: list) -> tuple:
            """(the id to show, the row that fell out from under him or None).

            Three cases in the order they matter: the posting he is on is still
            there; he has just acted on it, so the row that TOOK ITS PLACE is
            next (jumping back to the top made triaging a page cost O(n²)
            keystrokes); or it left the list without him asking, and then it
            stays in the reading pane rather than being silently replaced."""
            previous = state["selected"]
            if previous in fresh:
                return previous, None
            index = state["prefer_index"]
            if index is not None:
                # He acted on it himself, so it is meant to be gone: the row
                # that took its place is next, and an empty list is empty.
                return (new_order[min(index, len(new_order) - 1)]
                        if new_order else None), None
            if previous is not None and previous in shown:
                return previous, shown[previous]
            return (new_order[0] if new_order else None), None

        def render_rows(view: dict) -> None:
            rows_host.clear()
            row_elements.clear()
            with rows_host:
                if not order:
                    ui.label(view["view"].empty).classes("p-4 jd-meta")
                for job_id in order:
                    render_row(shown[job_id], view["siblings"])

        def render_row(job: dict, siblings: dict) -> None:
            # A div, NOT a button, and the difference is the whole keyboard.
            # `ui.keyboard`'s `ignore` is a CLIENT-side rule reading
            # document.activeElement, and a browser focuses a <button> on
            # mousedown — so with rows as buttons, clicking a posting to read
            # it silently killed j/k/x/s/o/Enter until something else took the
            # focus away. `role=option` inside the list's `role=listbox` is
            # also what makes `aria-selected` mean anything.
            row = ui.element("div").classes("jd-row").props(
                f'role=option '
                f'data-unread={"false" if job["opened_at"] else "true"} '
                f'aria-selected={"true" if job["id"] == state["selected"] else "false"}'
            )
            row_elements[job["id"]] = row
            row.on("click", lambda _=None, j=job["id"]: select(j))
            with row:
                ui.element("span").classes("jd-gutter")
                with ui.element("div").classes("jd-row-body"):
                    with ui.row().classes("items-baseline gap-2 no-wrap w-full"):
                        ui.label(job["company"] or "—").classes("jd-firma")
                        score = job["effective_score"]
                        ui.label("—" if score is None else str(score)).classes(
                            "jd-score ml-auto" + (" hi" if (score or 0) >= 80 else ""))
                    ui.label(clean_title(job["title"])).classes("jd-title")
                    ui.label(row_meta(job)).classes("jd-meta")
            others = job.get("company_count", 1) - 1
            if others > 0:
                stellen = "weitere Stelle" if others == 1 else "weitere Stellen"
                ui.label(f"↳ +{others} {stellen} bei dieser Firma") \
                    .classes("jd-siblings w-full")

        def render_pager(view: dict) -> None:
            pager.clear()
            with pager:
                if view["pages"] <= 1:
                    return
                ui.button(icon="chevron_left", on_click=lambda: turn_page(-1)) \
                    .props("flat dense").set_enabled(view["page"] > 0)
                ui.label(f"Seite {view['page'] + 1}/{view['pages']}") \
                    .classes("jd-meta")
                ui.button(icon="chevron_right", on_click=lambda: turn_page(1)) \
                    .props("flat dense") \
                    .set_enabled(view["page"] + 1 < view["pages"])

        def _render_siblings(job: dict) -> None:
            """The other postings this company has, with their own links.

            One row stands for a COMPANY because only one application per
            company is possible — but that makes choosing WHICH posting to
            apply with a real decision, and the row alone gives him nothing to
            decide with. One employer holds 27 of his postings; without this
            they are in the database and nowhere else."""
            group = siblings.get(job.get("company_key"), [])
            others = job.get("company_count", 1) - 1
            if others <= 0:
                return
            listed = len(group)
            more = "" if others <= listed else (
                f" — die {listed} bestbewerteten von {others}")
            stellen = "weitere Stelle" if others == 1 else "weitere Stellen"
            with ui.column().classes("gap-1 mt-6 pl-3 border-l w-full"):
                ui.label(f"{others} {stellen} bei {job['company']}{more} — eine "
                         f"Bewerbung pro Firma, deshalb steht oben die "
                         f"bestbewertete.").classes("jd-meta")
                for other in group:
                    with ui.row().classes("items-baseline gap-2 no-wrap"):
                        score = other["effective_score"]
                        ui.label("—" if score is None else str(score)) \
                            .classes("jd-score")
                        ui.label(clean_title(other["title"]) or other["title"]) \
                            .classes("text-sm")
                        other_url = _openable_url(other)
                        if other_url:
                            ui.button("öffnen",
                                      on_click=lambda u=other_url:
                                          ui.navigate.to(u, new_tab=True)) \
                                .props("flat dense no-caps")

        def render_reader(gone: bool = False) -> None:
            reader.clear()
            job = shown.get(state["selected"])
            with reader:
                if job is None:
                    ui.label("Keine Anzeige ausgewählt.").classes("p-6 jd-meta")
                    return
                if gone:
                    ui.label("Diese Anzeige ist gerade aus dieser Ansicht "
                             "gefallen — du liest sie weiter, sie steht aber "
                             "jetzt in einer anderen Ansicht.") \
                        .classes("jd-note warn m-6 mb-0")
                _render_reader(job, applied.get(job["id"]))

        def _render_reader(job: dict, already: dict | None) -> None:
            with ui.column().classes("w-full gap-0 p-6"):
                ui.label(job["company"] or "—").classes("text-xl jd-serif")
                ui.label(clean_title(job["title"])).classes("text-base mt-1")
                ui.label(row_meta(job) + _score_line(job)).classes("jd-meta mt-2")
                # Triage first: the three things he does WITHOUT reading, so
                # they are reachable before the text and again from the
                # keyboard. The one action that commits him waits below it.
                with ui.row().classes("gap-2 my-4 flex-wrap"):
                    marked = bool(job["bookmarked_at"])
                    ui.button("★ gemerkt" if marked else "☆ merken",
                              on_click=lambda j=job["id"]: toggle_bookmark(j)) \
                        .props("flat dense no-caps")
                    if job["status"] == "skipped":
                        ui.button("↩ zurück in die Arbeitsliste",
                                  on_click=lambda j=job["id"]: revive(j)) \
                            .props("flat dense no-caps")
                    else:
                        ui.button("✕ kein Interesse",
                                  on_click=lambda j=job["id"]:
                                      not_interested(j)) \
                            .props("flat dense no-caps") \
                            .set_enabled(job["status"] == "new")
                    open_url = _openable_url(job)
                    if open_url:
                        ui.button("Anzeige öffnen",
                                  on_click=lambda u=open_url:
                                      ui.navigate.to(u, new_tab=True)) \
                            .props("flat dense no-caps")
                    if job["status"] == "new" and not job["contact_email"]:
                        ui.button("Kontakt-E-Mail suchen",
                                  on_click=lambda j=job: find_email(j)) \
                            .props("flat dense no-caps")
                for text, kind in reader_notes(job, already):
                    ui.label(text).classes(f"jd-note {kind} mb-2")
                _render_facts(job)
                description = job["description"] or "(keine Beschreibung)"
                ui.markdown(posting_markdown(description[:DESCRIPTION_LIMIT])) \
                    .classes("jd-ad mt-4")
                if len(description) > DESCRIPTION_LIMIT:
                    # The screen exists to show the whole advert. Where it
                    # cannot, it says so — the requirements section it cut may
                    # be the half that decides.
                    ui.label(
                        f"Der Text ist hier bei {DESCRIPTION_LIMIT:,}"
                        .replace(",", ".") +
                        f" von {len(description):,}".replace(",", ".") +
                        " Zeichen abgeschnitten — der Rest steht beim Anbieter."
                    ).classes("jd-note warn mt-3")
                if scoring.looks_like_snippet(job["description"] or ""):
                    ui.label(
                        f"Diese Quelle liefert nur einen Ausschnitt "
                        f"({len(job['description'] or '')} Zeichen) — der "
                        f"vollständige Text steht beim Anbieter."
                    ).classes("jd-note mt-3")
                # AFTER the text, deliberately: he reads the advert first and
                # only then sees what the machine made of it, and only then is
                # offered the one action that commits him.
                if job["match_reason"]:
                    with ui.element("div").classes("jd-why"):
                        ui.label(_verdict_heading(job)).classes("jd-meta")
                        ui.label(job["match_reason"]).classes("text-sm mt-1")
                _render_primary(job, already)
                _render_siblings(job)

        def _render_facts(job: dict) -> None:
            with ui.element("div").classes("jd-facts mt-4"):
                for key, value in reader_facts(job):
                    ui.label(key).classes("k")
                    ui.label(value)

        def _render_primary(job: dict, already: dict | None) -> None:
            action = primary_action(job, already)
            with ui.row().classes("items-center gap-3 mt-6 flex-wrap"):
                button = ui.button(action.label).props("no-caps")
                button.set_enabled(action.enabled)
                if action.enabled:
                    button.on_click(
                        lambda _=None, j=job, a=action, b=button: run_action(a, j, b))
                if action.reason:
                    ui.label(action.reason).classes("jd-reason")
                if _wants_a_letter(job, already):
                    # The cockpit parks beside the employer's form and lists
                    # what is missing — but it cannot write an Anschreiben, and
                    # the German market is form-first (28 of his 65
                    # applications went that way). Without this the only route
                    # left was the batch button in Settings.
                    letter = ui.button("Anschreiben schreiben") \
                        .props("outline no-caps")
                    letter.on_click(
                        lambda _=None, j=job, b=letter: draft(j, button=b))

        # ------------------------------------------------------------------
        # what the controls do
        # ------------------------------------------------------------------
        async def select(job_id: int) -> None:
            """Open a posting in the reading pane, and record that he read it."""
            if job_id not in shown:
                return
            state["selected"] = job_id
            read_here.add(job_id)
            if not shown[job_id]["opened_at"]:
                # Recorded on the row we are holding too, so the mark and the
                # database agree without asking it again.
                shown[job_id]["opened_at"] = await run.io_bound(
                    _mark_opened, job_id)
                # …and the watcher is told, or his own click would look like
                # somebody else's write on the next tick and rebuild the page
                # under the advert he has just started reading. `opened_at` is
                # part of the shared signature, so EVERY first open did this.
                live_view.mark(await run.io_bound(_signature))
            restamp_rows()
            render_reader()

        def restamp_rows() -> None:
            """Re-stamp the selection and unread marks without rebuilding.

            A rebuild on every keystroke would throw the list's scroll position
            away, and moving down the list is the thing he does most."""
            for job_id, element in row_elements.items():
                element.props(
                    f'data-unread={"false" if shown[job_id]["opened_at"] else "true"} '
                    f'aria-selected={"true" if job_id == state["selected"] else "false"}'
                )
                element.update()

        async def move(step: int) -> None:
            if not order:
                return
            if state["selected"] in order:
                index = order.index(state["selected"]) + step
            else:
                index = 0
            await select(order[min(max(index, 0), len(order) - 1)])

        async def turn_page(step: int) -> None:
            state["page"] += step
            state["selected"] = None
            await refresh(force=True)

        async def set_view(value: str) -> None:
            """Move to another named view.

            One assignment and one refresh: nothing here writes another control,
            so no handler can be echoed back into this one — which is what made
            the two pile switches rebuild the page forever."""
            state["view"] = value or DEFAULT_VIEW
            state["page"] = 0  # a different list: page 3 of it means nothing
            state["selected"] = None
            read_here.clear()   # coming back to Neu is when it should have emptied
            await refresh(force=True)

        async def set_search(value: str) -> None:
            state["search"] = value
            state["page"] = 0
            state["selected"] = None
            await refresh(force=True)

        async def ask_before_spending(action: Action, job: dict) -> bool:
            """The keyboard asks before an action costs money.

            ⏎ is the one control on this screen whose meaning changes with the
            row under the cursor: he moves with j and presses it expecting
            "open this", and on a direct-e-mail posting that is a Sonnet call
            of about nine cents and forty seconds. A button he clicked carries
            its own label and needs no second question."""
            if action.key != ACTION_DRAFT:
                return True
            overlay.clear()
            with overlay, ui.dialog() as confirm, ui.card():
                ui.label("Bewerbung schreiben?").classes("font-bold")
                ui.label(f"{job['company']} — {clean_title(job['title'])}") \
                    .classes("text-sm")
                ui.label("Das schreibt der KI-Dienst, kostet Geld und dauert "
                         "etwa eine Minute.").classes("text-sm text-gray-600")
                with ui.row().classes("justify-end gap-2 w-full"):
                    ui.button("Abbrechen",
                              on_click=lambda: confirm.submit(False)) \
                        .props("flat no-caps")
                    ui.button("Schreiben",
                              on_click=lambda: confirm.submit(True)) \
                        .props("no-caps")
            confirm.open()
            return bool(await confirm)

        async def run_action(action: Action, job: dict, button) -> None:
            if action.key == ACTION_RESOLVE:
                await resolve_channel(job)
            elif action.key == ACTION_REVIVE:
                await revive(job["id"])
            elif action.key == ACTION_OPEN:
                url = _openable_url(job)
                with overlay:
                    if url:
                        ui.navigate.to(url, new_tab=True)
                    else:
                        say("Diese Anzeige hat keine Adresse, die geöffnet "
                            "werden kann.", type="warning")
            elif action.key == ACTION_DRAFT:
                await draft(job, button=button)
            elif action.key == ACTION_FORM:
                # From `overlay`, which is a sibling of both panes: a handler
                # runs in the slot of the element that fired it, and a refresh
                # racing this one would delete that slot before the navigation
                # is built.
                with overlay:
                    ui.navigate.to(f"/cockpit/{job['id']}")
            elif action.key == ACTION_QUEUE:
                with overlay:
                    ui.navigate.to("/queue")
            elif action.key == ACTION_RECORD:
                await confirm_applied(job)

        async def on_key(event) -> None:
            if not event.action.keydown or event.action.repeat:
                # Held keys repeat ~30 times a second. Without this, holding ⏎
                # on an unresolved posting fires a burst of concurrent channel
                # resolutions at one employer's host, and holding x walks a
                # dozen postings out of the list.
                return
            if event.modifiers.ctrl or event.modifiers.meta or event.modifiers.alt:
                return
            if live.dialog_open():
                # A dialog sits OVER the list. Without this, reaching for the
                # close button and hitting 'x' skipped the posting underneath
                # it — and the refresh that followed rebuilt the list beneath
                # the still-open dialog, so nothing on screen even flickered.
                return
            key = str(event.key)
            job = shown.get(state["selected"])
            if key == "j" or key == "ArrowDown":
                await move(1)
            elif key == "k" or key == "ArrowUp":
                await move(-1)
            elif job is None:
                return
            elif key == "x":
                await not_interested(job["id"])
            elif key == "s":
                await toggle_bookmark(job["id"])
            elif key == "o":
                url = _openable_url(job)
                if url:
                    with overlay:
                        ui.navigate.to(url, new_tab=True)
            elif key == "Enter":
                action = primary_action(job, applied.get(job["id"]))
                if action.enabled and await ask_before_spending(action, job):
                    await run_action(action, job, None)

        def _hold_place(job_id: int) -> None:
            """Remember where in the list he acted, so the row that takes that
            posting's place is the one he lands on."""
            state["prefer_index"] = order.index(job_id) if job_id in order else None

        async def toggle_bookmark(job_id: int) -> None:
            job = shown.get(job_id)
            if job is None:
                return
            _hold_place(job_id)
            await run.io_bound(_set_bookmark, job_id, not job["bookmarked_at"])
            await refresh(force=True)

        async def revive(job_id: int) -> None:
            """Put a posting he had put away back into the working list."""
            job = shown.get(job_id)
            if job is None or job["status"] != "skipped":
                return
            _hold_place(job_id)
            await run.io_bound(_set_status, job_id, "new")
            await refresh(force=True)

        async def not_interested(job_id: int) -> None:
            job = shown.get(job_id)
            if job is None or job["status"] != "new":
                return
            _hold_place(job_id)
            await run.io_bound(_set_status, job_id, "skipped")
            await refresh(force=True)

        # ------------------------------------------------------------------
        # the slow actions, unchanged in substance from the old inbox
        # ------------------------------------------------------------------
        def show_draft(draft_row: dict, job: dict):
            with overlay, ui.dialog() as dialog, \
                    ui.card().classes("w-[720px] max-w-full"):
                ui.label(f"Entwurf — {clean_title(job['title'])}") \
                    .classes("font-bold")
                recipient = draft_row["recipient"] or \
                    "keine Bewerbungs-E-Mail gefunden (Formular oder Kontakt fehlt)"
                ui.label(f"An: {recipient}").classes("text-sm text-gray-600")
                ui.input("Betreff", value=draft_row["betreff"]) \
                    .classes("w-full").props("readonly")
                ui.textarea("E-Mail", value=draft_row["email_body"]) \
                    .classes("w-full").props("readonly autogrow")
                ui.textarea("Anschreiben", value=draft_row["anschreiben_body"]) \
                    .classes("w-full").props("readonly autogrow")
                ui.label(
                    f"Modell: {draft_row['llm_model']} · bearbeiten und senden "
                    f"in der Review queue"
                ).classes("text-xs text-gray-500")
                pdf_label = ui.label(
                    f"Mappe: {draft_row['pdf_path']}" if draft_row["pdf_path"]
                    else ""
                ).classes("text-xs text-gray-600")
                with ui.row().classes("w-full justify-end gap-2"):
                    async def make_pdf():
                        say("Bewerbungsmappe wird gebaut…")
                        result = await mappe.create_mappe(job["id"])
                        if not result["ok"]:
                            say(result["error"], type="warning", multi_line=True)
                            return
                        pdf_label.set_text(f"Mappe: {result['pdf_path']}")
                        say(helpers.mappe_summary(result, with_anlagen=True),
                            type="positive", multi_line=True)
                        if result["warning"]:
                            say(result["warning"], type="warning", multi_line=True)

                    def open_pdf():
                        path = (pdf_label.text or "").removeprefix("Mappe: ")
                        if not path:
                            say("die Mappe zuerst bauen", type="warning")
                        elif not pathlib.Path(path).exists():
                            say("die Mappe-Datei ist weg — neu bauen",
                                type="warning")
                        else:
                            open_in_system(path)

                    ui.button("Mappe bauen", icon="picture_as_pdf",
                              on_click=make_pdf).props("outline no-caps")
                    ui.button("Mappe öffnen", icon="open_in_new",
                              on_click=open_pdf).props("outline no-caps")
                    ui.button("Neu schreiben", icon="refresh",
                              on_click=lambda: redraft(dialog, job)) \
                        .props("outline no-caps")
                    ui.button("Review queue", icon="outbox",
                              on_click=lambda: ui.navigate.to("/queue")) \
                        .props("outline no-caps")
                    ui.button("Schließen", on_click=dialog.close) \
                        .props("flat no-caps")
            dialog.open()

        async def redraft(dialog, job: dict):
            dialog.close()
            await draft(job, force=True)

        async def draft(job: dict, force: bool = False, button=None):
            # a finished draft costs nothing to show again — regenerate only
            # on explicit request
            if not force:
                existing = await run.io_bound(_load_draft, job["id"])
                if existing is not None and existing["status"] == "ready":
                    overlay.clear()
                    with overlay:
                        show_draft(existing, job)
                    return
            say("Bewerbung wird geschrieben…")
            if button is not None:
                # A minute is long enough that a faded toast reads as "nothing
                # happened" — the button that was pressed says what it is doing.
                button.set_text("wird geschrieben …")
                button.disable()
            try:
                result = await drafting.draft_for_job(job["id"])
            except Exception:  # noqa: BLE001 — one posting, not the page
                # drafting re-raises anything that is not an LLM error, having
                # already marked the draft 'failed'. Without this the relabelled
                # button stayed dead and the row kept claiming a draft was being
                # written, with nothing said anywhere the user looks.
                log.exception("drafting job %s raised", job["id"])
                result = {"ok": False, "draft": None,
                          "error": "Der Entwurf ist unerwartet fehlgeschlagen "
                                   "— Details stehen im Log."}
            # The reader carries the outcome from here on, whichever way it went
            # — and everything the user sees afterwards is built in `overlay`,
            # because this refresh has just deleted the button we came from.
            await refresh(force=True)
            overlay.clear()
            with overlay:
                if not result["ok"]:
                    say(result["error"], type="warning", multi_line=True)
                    return
                show_draft(result["draft"], job)

        async def resolve_channel(job: dict):
            say("Bewerbungskanal wird ermittelt…")
            res = await apply_resolve.resolve_and_store(job["id"])
            label = _apply_line({**job, "apply_channel": res["channel"],
                                 "ats_vendor": res["vendor"]})
            await refresh(force=True)
            say(label or "Kanal nicht ermittelbar",
                type="positive" if label else "warning")

        async def find_email(job: dict):
            say("Kontakt-E-Mail wird gesucht…")
            res = await contact_lookup.lookup_and_propose(job["id"])
            if not res["email"]:
                say("Keine verifizierte Bewerbungs-E-Mail gefunden",
                    type="warning")
                return
            overlay.clear()
            with overlay, ui.dialog() as dialog, \
                    ui.card().classes("w-[440px] max-w-full"):
                ui.label("Gefundene Bewerbungs-E-Mail").classes("font-bold")
                ui.label(res["email"]).classes("text-lg")
                ui.label(f"Quelle: {res['source_url']}").classes(
                    "text-xs text-gray-500")
                if res["generic"]:
                    ui.label("⚠ Allgemeine Adresse (info@) — nicht für den "
                             "Auto-Versand.").classes("text-sm text-amber-700")
                elif not res["dedicated"]:
                    ui.label("Persönliche Adresse — bitte vor dem Versand "
                             "prüfen.").classes("text-sm text-gray-600")

                async def adopt():
                    await run.io_bound(_set_contact_email, job["id"], res["email"])
                    say(f"Übernommen: {res['email']}", type="positive")
                    dialog.close()
                    await refresh(force=True)

                with ui.row().classes("w-full justify-end gap-2"):
                    if not res["generic"]:  # never adopt a generic info@ inbox
                        ui.button("Übernehmen", icon="check",
                                  on_click=adopt).props("color=positive no-caps")
                    ui.button("Schließen", on_click=dialog.close) \
                        .props("flat no-caps")
            dialog.open()

        async def confirm_applied(job: dict):
            bewerbung_id = await run.io_bound(_confirm_applied, job["id"],
                                              "Online-Portal")
            await refresh(force=True)
            if bewerbung_id is None:
                say("Blockiert: bei dieser Firma liegt schon eine Bewerbung",
                    type="warning")
            else:
                say("Bewerbung eingetragen ✓", type="positive")

        # Postings arrive hourly, scores land every ten minutes and the liveness
        # pass runs 90 s after every start — all of it invisible until this. It
        # rebuilds only when the data really changed, and never while a dialog
        # is on screen: the reading pane survives a rebuild by itself, so unlike
        # the old expansions there is nothing here to yank out from under him.
        with chip_host:
            live_view = live.watch(_signature, refresh, busy=live.dialog_open)
        # `ignore` is NiceGUI's own client-side rule and the only one that can
        # work: a keystroke typed into the search box must never reach here, and
        # only the browser knows where the caret is. Pinned by a test, because
        # 'x' landing on the page while he types a company name would throw the
        # posting away.
        ui.keyboard(on_key=on_key, ignore=["input", "select", "button", "textarea"])
        await refresh(force=True)
