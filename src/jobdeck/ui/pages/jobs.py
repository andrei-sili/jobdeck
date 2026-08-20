"""Job inbox: discovered postings with per-job actions."""

import datetime
import logging
import math
import pathlib
from dataclasses import dataclass

from fastapi.responses import RedirectResponse
from nicegui import app, run, ui

from jobdeck import apply_channel, apply_form, config, db, freshness
from jobdeck.ai import scoring
from jobdeck.ai.drafting import clean_title
from jobdeck.constants import DRAFT_DELIVERED
from jobdeck.dedupe import duplicates_for_jobs, norm
from jobdeck.services import (
    apply_record,
    apply_resolve,
    contact_lookup,
    drafting,
    liveness,
    mappe,
    polling,
    preparing,
)
from jobdeck.services.replies import RECEIPT_WINDOW_H
from jobdeck.ui import draft_editor, helpers, live, rail
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
            "old": "exclude", "hidden": "exclude"}
# "Everything" still means everything he has not put out of sight: a hidden
# company is his standing decision, not a property of the posting, so only the
# view that exists to review those decisions asks for them.
_EVERYTHING = {"mismatches": "include", "gone": "include", "applied": "include",
               "old": "include", "hidden": "exclude"}


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
    # Nothing is deleted, only moved into a view with a name. This is that
    # view — and it is the only one that asks for hidden companies, so the
    # word "everything" everywhere else keeps meaning "everything you have not
    # put out of sight".
    View("ausgeblendet", "Ausgeblendete Firmen", "new",
         {**_EVERYTHING, "hidden": "only"},
         "Keine Firma ausgeblendet. Mit „x“ legst du eine hier ab."),
)
# What the floor control offers. 40 is the default because it is where the
# measurement puts the cut: his working list holds 300 postings and 222 of them
# score under 40, so this takes it to 78 — while 60 would take it to 52 and a
# list that loses 83 % of its rows on the first evening invites a third
# rejection. He can reach 60 with one press, and it stays where he leaves it.
SCORE_FLOORS = {0: "Alle", 40: "ab 40", 60: "ab 60", 70: "ab 70"}
DEFAULT_MIN_SCORE = 40

SORT_LABELS = {"date": "Neueste zuerst", "score": "Beste Treffer zuerst"}

DEFAULT_VIEW = VIEWS[0].key
_BY_KEY = {view.key: view for view in VIEWS}


def view_for(key: str) -> View:
    """The named view, falling back to the default rather than raising.

    The key reaches here from a control and one day from a URL; an unknown one
    is a screen he cannot open, and the default is always a safe answer."""
    return _BY_KEY.get(key, _BY_KEY[DEFAULT_VIEW])


# What the "Ansicht" control offers. The other seven views are still views —
# nothing is deleted, only moved into a view with a name — but they are piles,
# and a pile is found by its NUMBER under the list rather than by scrolling an
# eleven-item dropdown looking for a word. He has rejected this arrangement
# twice for having too much on screen.
MAIN_VIEW_KEYS = ("neu", "offen", "vorgemerkt", "in_arbeit",
                  # These three stay in the control because they have no
                  # number: they are decided by a STATUS rather than by one of
                  # the filter arms the line counts, so there is no door to
                  # click. A posting must always be findable somewhere.
                  "beworben", "kein_interesse", "doppelt")


def hidden_parts(view: View, counts: dict, stale_age_days: int,
                 search: str = "") -> list[dict]:
    """The pile line, as data: each part its own sentence and, where there is
    one, the view that opens it.

    Structured rather than a string so a number can be a DOOR. The counts were
    always printed and never clickable, which meant the piles were named on
    screen and reachable only through a dropdown that named them again.
    """
    """What this view is not showing, derived from the filters it actually used
    so the label cannot contradict the list. Independent statements, never a
    total: a posting can be a mismatch AND offline AND old.

    A search narrows the list without hiding a pile, so it is stated first and
    in its own words — otherwise the pile counts read as if they explained a
    three-row result.
    """
    parts: list[dict] = []
    if search.strip():
        parts.append({"text": f"gefiltert nach „{search.strip()}“",
                      "view": None})
    if view.filters.get("opened") == "exclude" and counts.get("read"):
        parts.append({"text": f"{counts['read']} schon gelesen", "view": "offen"})
    for arm, count_key, pile, hidden, only in (
        ("mismatches", "mismatches", "passt_nicht", "{n} passen nicht",
         "{n} verletzen eine harte Anforderung"),
        ("gone", "dead", "offline", "{n} offline", "{n} Anzeigen sind offline"),
        ("applied", "applied_firm", "firma_kontaktiert",
         "{n} bei schon beworbenen Firmen",
         "{n} Stellen bei Firmen, bei denen du dich schon beworben hast"),
        ("old", "old", "alt", f"{{n}} älter als {stale_age_days} Tage",
         f"{{n}} Anzeigen älter als {stale_age_days} Tage"),
        ("hidden", "hidden", "ausgeblendet", "{n} bei ausgeblendeten Firmen",
         "{n} Anzeigen bei ausgeblendeten Firmen"),
    ):
        count = counts.get(count_key, 0)
        if view.filters.get(arm) == "exclude" and count:
            parts.append({"text": hidden.format(n=count), "view": pile})
        elif view.filters.get(arm) == "only":
            parts.append({"text": only.format(n=count), "view": None})
    return parts


def _hidden_line(view: View, counts: dict, stale_age_days: int,
                 search: str = "") -> str:
    """The same line as plain text, for anything that wants one sentence."""
    return " · ".join(
        part["text"] for part in hidden_parts(view, counts, stale_age_days,
                                              search))


# ---------------------------------------------------------------------------
# One primary action per posting, and its whole state machine.
#
# 650 of his 803 postings sit in the FIRST state — the channel has never been
# resolved — so a screen offering "apply" everywhere would be wrong on four
# rows out of five. A blocked action states the reason NEXT TO ITSELF rather
# than in a tooltip: he reads this list with the keyboard, and a tooltip is a
# thing only a mouse can find.
# ---------------------------------------------------------------------------
_FORM_CHANNELS = (apply_channel.CHANNEL_ATS, apply_channel.CHANNEL_BOARD,
                  apply_channel.CHANNEL_COMPANY_SITE)


# ---------------------------------------------------------------------------
# Everything an application to THIS posting needs, as steps.
#
# One button was not enough. An application is three or four acts — write the
# letter, build the Mappe, send it or open the employer's form, record it — and
# which of them apply depends on whether the posting gave us an e-mail address.
# Hiding the rest behind other screens is what made recording a form
# application a detour through the cockpit.
# ---------------------------------------------------------------------------
STEP_RESOLVE = "resolve"
STEP_DRAFT = "draft"
STEP_MAPPE = "mappe"
STEP_SEND = "send"
STEP_START = "start"
STEP_RECORD = "record"


@dataclass(frozen=True)
class Step:
    """One act of applying: what it is called, whether it is done, and — when
    it cannot be pressed — why not."""

    key: str
    label: str
    done: bool = False
    enabled: bool = True
    reason: str = ""


def _blocking_reason(job: dict, already: dict | None) -> str:
    """Why no application can be made here at all, '' when one can."""
    status = str(job.get("status") or "")
    if status == "applied":
        return "Diese Anzeige ist schon als Bewerbung eingetragen."
    if already:
        return helpers.applied_line(already)
    if status == "duplicate":
        return ("Diese Anzeige gehört zu einer Firma, bei der schon eine "
                "Bewerbung liegt.")
    if status == "skipped":
        return "Du hattest sie weggelegt — erst zurück in die Arbeitsliste."
    return ""


def apply_steps(job: dict, already: dict | None = None) -> list[Step]:
    """The acts this posting needs, in the order they happen.

    By E-MAIL: write the letter, then check and send it — the editor builds the
    Mappe on the way. By FORM: ONE press that opens the employer's page and
    prepares everything behind it, then one press to say it went out. Nothing
    here fills a form; that is a settled decision.

    "Kanal ermitteln" is not a step any more. The scheduler resolves the whole
    backlog by itself every half hour, and a button whose entire content is
    "ask the question you already asked me to answer" rendered on four rows out
    of five.
    """
    blocked = _blocking_reason(job, already)
    draft_status = str(job.get("draft_status") or "")
    by_email = _by_email(job)
    # What may still be checked and sent from HERE. A draft that is sending or
    # sent is the record of what went out; resolving one belongs to the review
    # queue, which is the only screen that can tell the two apart.
    has_letter = draft_status in draft_editor.EDITABLE_STATUS
    # A letter that is with an employer, however it got there: neither can be
    # rewritten, and offering "Anschreiben neu schreiben" for one would offer
    # to rewrite the record of what went out.
    letter_gone = draft_status in ("sending", *DRAFT_DELIVERED)
    writing = (draft_status == "generating"
               and not drafting.claim_is_stale(job.get("draft_updated_at")))
    steps: list[Step] = []

    if by_email:
        steps.append(Step(
            STEP_DRAFT,
            "Anschreiben neu schreiben" if has_letter
            else "E-Mail-Bewerbung schreiben",
            done=has_letter or letter_gone,
            enabled=not blocked and not writing and not letter_gone,
            reason=blocked or ("Wird gerade geschrieben — etwa eine Minute."
                               if writing else ""),
        ))
        steps.append(Step(
            STEP_SEND, "Prüfen und senden", done=draft_status == "sent",
            enabled=has_letter and not blocked,
            reason=blocked or ("" if has_letter else
                               "Im Postausgang auflösen." if letter_gone
                               else "Erst das Anschreiben schreiben."),
        ))
    else:
        # ONE press, not four. It opens the employer's page and then does every
        # free step by itself — the letter, the complete Mappe, the file put
        # where their upload dialog will look for it. The old row was
        # Anschreiben → Mappe → Formular → eintragen, and he skipped straight
        # to the form: six of his eleven open applications had no letter,
        # which was the flow's doing rather than his preference.
        #
        # Blocked as before, and not for tidiness: the press STAMPS
        # `form_opened_at`, the app's record that an application has started —
        # at a company where one already exists, that is the one thing that
        # must not begin. Reading the advert stays a press away in the triage
        # row, and that one changes nothing.
        started = bool(job.get("form_opened_at"))
        no_url = not _openable_url(job)
        room = job.get("draft_room")
        steps.append(Step(
            STEP_START,
            f"Bewerbung starten · ~{_usd(preparing.COST_PER_DRAFT_USD)}",
            done=started,
            enabled=not blocked and not started and not no_url
                    and room is not False,
            reason=blocked or (
                "Diese Anzeige nennt keine Adresse zum Öffnen." if no_url
                else "" if started
                else "Das Tageslimit für Anschreiben ist aufgebraucht — "
                     "in den Einstellungen anheben." if room is False
                else ""),
        ))
    # Only once something was actually started. Without this the step is the
    # first ENABLED one whenever "Bewerbung starten" is refused — the daily
    # letter cap used up, or a posting with no openable address — so
    # "Abgeschickt" became the SOLID recommended button, and ⏎ under a cursor
    # moving down the list wrote a ledger row for a form that was never opened.
    # That row permanently spends the company's only application slot.
    started = bool(job.get("form_opened_at")) or by_email
    steps.append(Step(
        STEP_RECORD,
        "Abgeschickt" if not by_email else "Beworben eintragen",
        done=str(job.get("status") or "") == "applied",
        enabled=not blocked and started,
        reason=blocked or ("Drück das erst, wenn die Bewerbung wirklich raus ist."
                           if started else
                           "Erst die Bewerbung starten — oder von Hand "
                           "bewerben und dann hier eintragen."),
    ))
    return steps


def _usd(amount: float) -> str:
    """A price the way he reads one — the label IS the disclosure.

    A button he pressed carries its own figure; only ⏎ gets a dialog, which is
    the rule `ask_before_spending` already holds."""
    return f"{amount:.2f} $".replace(".", ",")


def _by_email(job: dict) -> bool:
    """Does an application to this posting go by e-mail?

    One definition, because two things read it: which steps the screen offers,
    and what the ledger records as the channel — and the ledger is the table
    the duplicate gate reads."""
    return bool(job.get("contact_email")
                or job.get("apply_channel") == apply_channel.CHANNEL_DIRECT_EMAIL)


def _next_step(steps: list[Step]) -> int:
    """Which step is the one to press now — the first that is neither done nor
    refused. Only that one is drawn solid; the rest stay available but quiet,
    so the screen suggests an order without forbidding another."""
    for index, step in enumerate(steps):
        if not step.done and step.enabled:
            return index
    return -1


def _score_line(job: dict, with_age: bool = True) -> str:
    """The score, and what its age did to it: ' · match 92 → 72 · 61 Tage alt'.

    Both numbers come from the row the query returned, so the one shown is the
    one that decided the position (see freshness.py). Showing the arrow only
    when age actually cost something keeps a fresh posting's line quiet.

    `with_age=False` where the age is already on the same line — the reader's
    header would otherwise read "61 T · … · 61 Tage alt"."""
    score = job["match_score"]
    if score is None:
        return ""
    age = job["age_days"]
    effective = job["effective_score"]
    if not with_age:
        aged = ""
    elif age is None:
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
               keep_ids: tuple[int, ...] = (), min_score: int = 0,
               sort: str = db.DEFAULT_LIST_ORDER) -> dict:
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
        # Whether a letter may still be written today. Read once and carried on
        # every row, so the button and the gate inside `drafting._claim` state
        # the same thing — a screen that offers what the gate below it refuses
        # is the defect this project keeps pinning.
        draft_room = db.count_drafts_today(con) < db.daily_draft_cap(con)
        filters = {**view.filters, "stale_age_days": stale_age_days,
                   "search": search, "keep_ids": keep_ids,
                   # Never in a pile view. "Passt nicht" IS the score-0 pile
                   # and "Ausgeblendete Firmen" is the hidden one — a floor or
                   # an exclusion applied there would empty the very view he
                   # opened to look at what was set aside.
                   "min_score": 0 if view.filters.get("mismatches") == "only"
                                else min_score,
                   "hidden": view.filters.get("hidden", "exclude"),
                   "sort": sort}
        total = db.count_job_groups(con, view.status, **filters)
        read_total = None
        if view.filters.get("opened") == "exclude":
            read_total = db.count_job_groups(
                con, view.status, **{**filters, "opened": "include"})
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        # True for every row of the pile view by construction, rather than a
        # per-row question: the view IS "only hidden companies", so asking the
        # database again once per row could only ever disagree with itself.
        in_hidden_view = filters["hidden"] == "only"
        rows = [{**dict(r), "draft_room": draft_room,
                 "company_hidden": in_hidden_view}
                for r in db.list_job_groups(
                    con, view.status, limit=PAGE_SIZE,
                    offset=page * PAGE_SIZE, **filters)]
        keys = [r["company_key"] for r in rows if r["company_count"] > 1]
        siblings: dict[str, list[dict]] = {}
        for row in db.list_company_siblings(con, keys, view.status, **filters):
            siblings.setdefault(row["company_key"], []).append(
                {**dict(row), "draft_room": draft_room,
                 "company_hidden": in_hidden_view})
        # Asked of the duplicate gate itself, for the rows on screen — see
        # dedupe.duplicates_for_jobs for why the stored flag cannot answer it.
        on_screen = rows + [r for group in siblings.values() for r in group]
        return {
            "signature": signature,
            "view": view,
            "applied": duplicates_for_jobs(con, on_screen),
            "rows": rows,
            "siblings": siblings,
            # Read outside every filter on purpose: an application under way is
            # not a search result. It has to be on screen whichever view he is
            # in, on whatever page, however he has searched — the whole point
            # is that the app cannot lose the posting he started.
            "started": [dict(r) for r in db.list_started_forms(con)],
            "counts": {
                "mismatches": db.count_mismatches(con, view.status),
                "dead": db.count_gone_jobs(con, view.status),
                "applied_firm": db.count_applied_firm_jobs(con, view.status),
                "old": db.count_old_jobs(con, view.status, stale_age_days),
                # What the landing view's own filter hides, counted in the same
                # unit as the list it labels: the difference between this view
                # and the same view with the read postings put back.
                "read": (read_total - total) if read_total is not None else 0,
                "hidden": db.count_job_groups(
                    con, view.status,
                    **{**filters, "hidden": "only", "min_score": 0}),
            },
            "poll": polling.last_poll(con),
            "search": search,
            "stale_age_days": stale_age_days,
            "total": total,
            "page": page,
            "pages": pages,
        }


# After this long, an entry stops reporting its age and starts asking the
# question. Not a dialog and not a dismissible prompt: he learns to close those,
# and the app genuinely cannot see whether he pressed the employer's submit.
ASK_AFTER_MIN = 10
# And after this long it is worth noticing from across the screen.
STALE_FORM_H = 24


def _started_at(stamp: str) -> datetime.datetime | None:
    """When a form was opened, or None when the app cannot know.

    None is a real answer here, not a parse failure to paper over: eleven
    postings were mid-application when this shipped and three left no evidence
    of when, so they carry a named sentinel that must never become a date."""
    try:
        return datetime.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


def started_line(job: dict, now: datetime.datetime | None = None) -> str:
    """One running application, as the strip says it.

    Under ten minutes it reports; past ten it asks. The question is the LABEL
    rather than a prompt, so there is nothing to dismiss and therefore nothing
    to learn to dismiss — and since replies are read, an arriving
    Eingangsbestätigung closes it by itself. Past the receipt window (the
    SAME number the reader matches receipts within — two claims from one
    constant, or a receipt at 60 hours falls between them) the strip stops
    implying one is coming: plenty of German portals send nothing, and the
    entry is his to close.

    A form opened before the app could stamp it says so. Eleven of his
    postings were in that state and three left no evidence of when; giving
    those a computed age would sort them among applications begun this minute.
    """
    company = str(job.get("company") or "—")
    stamp = str(job.get("form_opened_at") or "")
    when = _started_at(stamp)
    if when is None:
        return f"Formular bei {company} — seit unbekannt"
    now = now or datetime.datetime.now()
    minutes = max(0, int((now - when).total_seconds() // 60))
    if minutes >= RECEIPT_WINDOW_H * 60:
        return (f"Formular bei {company} — abgeschickt? Keine "
                f"Eingangsbestätigung nach {RECEIPT_WINDOW_H // 24} Tagen; "
                f"viele Portale schicken keine.")
    if minutes >= ASK_AFTER_MIN:
        return f"Formular bei {company} — abgeschickt?"
    if minutes < 1:
        return f"Formular bei {company} — gerade eben"
    return f"Formular bei {company} — seit {minutes} Min."


def mappe_line(job: dict) -> tuple[str, str]:
    """What the strip says about the documents, and how loudly.

    `mappe_kind` is written by the build, so "nothing is staged" is a fact
    rather than a guess. It has to be said out loud: a Bewerbungsmappe is
    always complete by his decision, so an incomplete one offered silently to
    an upload button is the worst outcome this flow can produce.
    """
    if job.get("mappe_kind"):
        return "Mappe bereit", ""
    if str(job.get("draft_status") or "") == "generating":
        return "Mappe wird gebaut …", ""
    return "Mappe NICHT fertig — von Hand hochladen", "warn"


def _range_line(page: int, total: int, shown: int) -> str:
    """'51–100 von 266 Firmen' — where in the pipeline this page sits.

    The unit is named because a row is a COMPANY while the pile counts beside
    it are POSTINGS, and an unlabelled pair of numbers invites comparing them."""
    if not total:
        return ""
    first = page * PAGE_SIZE + 1
    return f"{first}–{first + shown - 1} von {total} Firmen"


# What this screen states that lives in app_settings rather than in a table.
# `db.data_signature` reads tables only, by design — so a screen that also
# states a SETTING has to sign it here, the way the Unterlagen screen does.
# Without this the button keeps saying "Tageslimit aufgebraucht" after he has
# raised the limit in Einstellungen, and after midnight, until he reloads.
_WATCHED_SETTINGS = ("daily_draft_cap", "drafts_written_count",
                     "drafts_written_date",
                     # The search line states these, and none of them is a row
                     # any table signature can see.
                     polling.LAST_POLL_AT, polling.LAST_POLL_REPORT,
                     polling.LAST_POLL_SOURCE)


def _whole(value) -> int:
    """A control's value as a whole number, 0 for anything that is not one."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def poll_line(report: dict, now: datetime.datetime | None = None) -> str:
    """What the last search found, as the one sentence above the list.

    The first word answers "cind?" — whether it was him or the schedule — which
    is half of what he was asking. Written as a pure function of the report so
    what the screen claims can be pinned without a browser.

    An empty report says the engine has never run rather than printing zeros:
    "0 neue Anzeigen" is a result, and no search is not a result.
    """
    if not report.get("at"):
        return "Noch nie gesucht."
    who = ("Von dir gestartet" if report.get("by") == "user"
           else "Automatisch gesucht")
    parts = [f"{report['new']} neue Anzeigen" if report["new"] != 1
             else "1 neue Anzeige"]
    if report.get("known"):
        parts.append(f"{report['known']} schon bekannt")
    if report.get("duplicate"):
        # The check he asked for by name — "la căutare, programul verifică
        # DB-ul ca să nu aducă iar firme la care a trimis". It has always
        # happened at ingestion; it has never been said out loud.
        parts.append(f"{report['duplicate']} bei Firmen, bei denen du dich "
                     f"schon beworben hast")
    return f"{who} {rail.clock(report['at'])} — " + " · ".join(parts)


def _hide_company(company: str) -> dict:
    """Put a company out of sight, and answer with what that cost.

    The figures come back with it because the undo bar has to NAME the price:
    "26 Anzeigen, die beste mit Bewertung 74" is a decision, "ausgeblendet" is
    a guess. Counted before the write, so they describe what was on screen.
    """
    with db.db() as con:
        rows = db.list_jobs(con, status="new", limit=1000,
                            search="", hidden="include")
        key = norm(company or "")
        mine = [r for r in rows if norm(r["company"] or "") == key]
        best = max((r["effective_score"] or 0) for r in mine) if mine else 0
        stored = db.hide_company(con, company)
    return {"ok": bool(stored), "key": stored, "company": company,
            "jobs": len(mine), "best": best}


def _unhide_company(company_key: str) -> None:
    with db.db() as con:
        db.unhide_company(con, company_key)


def _hidden_companies() -> list[dict]:
    with db.db() as con:
        return [dict(row) for row in db.list_hidden_companies(con)]


def _poll_report() -> dict:
    with db.db() as con:
        return polling.last_poll(con)


def _signature() -> tuple:
    """One cheap read of everything this page's rows can say (see ui/live.py)."""
    with db.db() as con:
        return (*db.data_signature(con),
                *(db.get_setting(con, key, "") for key in _WATCHED_SETTINGS))


def _set_status(job_id: int, status: str):
    with db.db() as con:
        db.set_job_status(con, job_id, status)


def _record_application(job_id: int, kanal: str) -> dict:
    """Write the application through the one recorder both this screen and the
    reply reader use. A module-level def rather than the service function
    passed straight to `run.io_bound`: the arity rule that catches a call with
    the wrong number of arguments only inspects targets defined in this file,
    and a TypeError raised in a worker thread is one log line and a button that
    looks alive."""
    return apply_record.record_application(job_id, kanal)


def _undo_application(job_id: int, bewerbung_id: int, previous_status: str):
    apply_record.undo(job_id, bewerbung_id, previous_status)


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


def _mark_form_opened(job_id: int) -> str:
    """Record that he opened this employer's form; answer with the stamp.

    Like `_mark_opened`: the page holds the row it is showing, and handing it
    the value that was actually written keeps that copy and the database saying
    the same thing without a second query."""
    with db.db() as con:
        db.mark_form_opened(con, job_id)
        row = db.get_job(con, job_id)
        return "" if row is None else str(row["form_opened_at"] or "")


def _short(value: str, limit: int = 60) -> str:
    """A label that fits. Only the LABEL is shortened — what reaches the
    clipboard is always the whole value, or a truncated Referenznummer would
    go into someone's form."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _form_data(job_id: int) -> dict | None:
    """The form answers for one posting, read in one go."""
    with db.db() as con:
        job = db.get_job(con, job_id)
        if job is None:
            return None
        draft = db.get_draft_by_job(con, job_id)
        settings = {key: db.get_setting(con, key, "")
                    for key in apply_form.APPLICANT_SETTINGS}
    return {
        "company": str(job["company"] or "—"),
        "personal": apply_form.personal_fields(settings),
        # `usable()` explicitly: `posting_fields` does NOT apply it, so a
        # discarded or failed letter would be offered as a form answer — words
        # he threw away, in someone's application.
        "posting": apply_form.posting_fields(
            dict(job), apply_form.usable(dict(draft) if draft else None)),
    }


def _staged_name(job_id: int) -> str:
    """The file name an employer's dialog will show, '' when none is staged."""
    with db.db() as con:
        job = db.get_job(con, job_id)
    path = str((job["upload_path"] if job is not None else "") or "")
    return pathlib.Path(path).name if path else ""


def _open_upload_folder() -> None:
    """Open the one folder an employer's file picker will land in.

    The folder, not the file: what has to change is where the DIALOG opens,
    and a file manager pointed at a directory is what teaches it."""
    helpers.open_in_system(str(config.UPLOAD_DIR))


def _clear_form_opened(job_id: int):
    """Through the service, so the staged file goes with the stamp — the
    database layer cannot unlink, and blanking the pointer first would leave
    the Mappe in the upload folder with nothing able to find it."""
    apply_record.abandon_form(job_id)


def _set_bookmark(job_id: int, marked: bool):
    with db.db() as con:
        db.set_bookmark(con, job_id, marked)


def _set_contact_email(job_id: int, email: str):
    with db.db() as con:
        db.set_contact_email(con, job_id, email, "web_lookup")


# What the "Kontaktdaten" dialog offers, in the order a German letter head
# reads them. The label is what he sees; the key is the column.
CONTACT_LABELS = (
    ("contact_email", "Bewerbungs-E-Mail"),
    ("ansprechpartner", "Ansprechpartner (z. B. Herr Dirk Muster)"),
    ("contact_strasse", "Straße und Hausnummer"),
    ("contact_plz_ort", "PLZ und Ort"),
    ("contact_phone", "Telefon (optional)"),
)


def _contact_values(job_id: int) -> dict:
    """The stored contact fields, read fresh when the dialog opens.

    Read here rather than taken from the row on screen: the dialog is also the
    correction path, and a row drawn minutes ago would quietly offer him a
    stale address to re-save over a newer one."""
    with db.db() as con:
        row = db.get_job(con, job_id)
    return {key: (row[key] if row is not None else "") or ""
            for key, _ in CONTACT_LABELS}


def _save_contact_values(job_id: int, values: dict):
    with db.db() as con:
        db.set_contact_details(con, job_id, values)


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
    "ready": ("✓ Entwurf fertig — im Postausgang prüfen und senden.",
              "text-sm text-green-700"),
    "approved": ("✓ Entwurf freigegeben — wartet im Postausgang auf den "
                 "Versand.", "text-sm text-green-700"),
    "sending": ("Ein Versand läuft — oder er ist stecken geblieben. Im "
                "Postausgang auflösen.", "text-sm text-amber-700"),
    "failed": ("⚠ Der Entwurf ist fehlgeschlagen — neu schreiben, oder im "
               "Postausgang verwerfen.", "text-sm text-red-700"),
    "sent": ("✓ Bewerbung gesendet.", "text-sm text-gray-600"),
    # The form path's counterpart to 'sent'. Without it a posting whose letter
    # went out inside an uploaded Mappe said nothing at all on its row.
    "filed": ("✓ Anschreiben mit der Bewerbung eingereicht.",
              "text-sm text-gray-600"),
}


# A claim whose process died says so instead of promising a minute forever —
# and it must say the SAME thing the review queue says about the same row, so
# both ask services/drafting. This is also the line that has to stay honest
# about the Draft button below it: the button is what restarts an abandoned
# draft, so it comes back exactly when this text starts calling it abandoned.
_CLAIM_LIVE = ("✍ Die Bewerbung wird gerade geschrieben — das dauert etwa "
               "eine Minute.", "text-sm text-blue-700")
_CLAIM_ABANDONED = ("⚠ Der Entwurf wurde begonnen und nie fertig — der "
                    "Vorgang ist abgebrochen. „Erneut schreiben“ drücken.",
                    "text-sm text-amber-700")


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
        # Two different facts, and the row says which one it has: the board's
        # publication date where it states one, and otherwise the day this app
        # first saw the posting. Printing the second under "Gefunden" was fine;
        # printing the FIRST under it was not.
        (("Veröffentlicht" if job.get("published_on") else "Gefunden"),
         str(job.get("published_on") or job.get("fetched_at") or "")[:10] or "—"),
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
    drawn = tuple(job.get(key) for key in (
        "id", "company", "title", "effective_score", "match_score", "age_days",
        "match_reason", "status", "apply_channel", "ats_vendor", "apply_url",
        "url", "contact_email", "draft_status", "draft_updated_at", "pdf_path",
        "liveness",
        "opened_at", "bookmarked_at", "temp_agency", "salary_from", "salary_to",
        "salary_period", "description", "refnr", "location", "published_on",
        "fetched_at", "source", "company_count", "company_key",
        # Anything the reading pane STATES has to be in here or it is never
        # redrawn: he presses the button, the write lands, and the pane goes on
        # offering the press he already made until he clicks another row.
        "form_opened_at", "upload_path", "mappe_kind", "draft_room",
        # The contact fields decide the button's own label and the channel
        # line beside it, so a save he just made has to move this tuple.
        "ansprechpartner", "contact_strasse", "contact_plz_ort",
    ))
    # The probe stamp is drawn only inside the offline warning, and the daily
    # liveness pass re-stamps hundreds of rows in a couple of minutes. Counting
    # it always would rebuild the advert he is reading once every thirty
    # seconds for the whole length of that pass, changing nothing on screen.
    return drawn + ((job.get("liveness_checked_at"),)
                    if job.get("liveness") == liveness.LIVENESS_GONE else ())


def _openable_url(job: dict) -> str:
    """The URL a posting's buttons may hand to the browser, '' when none is
    safe. The resolved apply link wins over the raw feed URL."""
    return openable_url(job.get("apply_url") or job.get("url") or "")


@app.get("/jobs")
def legacy_jobs_page():
    """The inbox's old address. Postings are the screen he opens the app for,
    so they took the home route; a bookmark or an old link still lands.

    An HTTP redirect rather than a page that draws nothing and then navigates:
    the moved address should answer as moved, and a page whose only job is to
    jump renders an empty screen first."""
    return RedirectResponse(STELLEN_PATH)


@app.get("/cockpit/{job_id}")
def legacy_cockpit_page(job_id: int):
    """The apply cockpit's old address.

    The cockpit was a second screen to stand beside an employer's form; the
    posting itself is that place now, so the screen is gone and its answers
    live on the running application. Same redirect as `/jobs` for the same
    reason — an old tab or bookmark should land somewhere real."""
    return RedirectResponse(STELLEN_PATH)


@ui.page(STELLEN_PATH)
async def jobs_page():
    async with frame("Stellen", current="stellen", padded=False):
        state = {"view": DEFAULT_VIEW, "page": 0, "search": "",
                 "selected": None, "prefer_index": None,
                 # His two filter controls. Held on the page rather than in
                 # app_settings: they are how he is looking right now, not a
                 # preference, and a stored one would silently outlive the
                 # evening that made sense of it.
                 "min_score": DEFAULT_MIN_SCORE,
                 "sort": db.DEFAULT_LIST_ORDER}
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
        # The strip's own rows and its age labels. Separate from `shown`: an
        # application under way is not part of the list and survives every
        # filter, page and search.
        strip_rows: dict[int, dict] = {}
        age_labels: dict[int, object] = {}
        refresh_gen = {"n": 0}   # rapid view flips: last request wins

        # Feedback has to outlive the row that asked for it. A handler runs in
        # the slot of the element that fired it, and a refresh clears the list —
        # so a dialog or notification built afterwards has no live parent and
        # raises instead of appearing, silently swallowing the very error it was
        # meant to report. This host is a sibling of both panes.
        overlay = ui.column().classes("contents")
        # A slot NOTHING clears, for one-shot timers. `overlay` is emptied at
        # the top of every handler, and NiceGUI reads a timer's parent slot
        # BEFORE its own stop check — so a one-shot parked in `overlay` whose
        # slot is cleared before it fires writes an ERROR traceback into his
        # log instead of quietly stopping. Observed live, twice in one session,
        # and the same class as the queue timer that logged on every leave.
        timers = ui.column().classes("contents")

        def say(message: str, **kwargs) -> None:
            """Tell the user something, from a slot no refresh can delete."""
            with overlay:
                ui.notify(message, **kwargs)

        with ui.column().classes("jd-screen w-full"):
            with ui.row().classes("jd-strip"):
                ui.label("Stellen").classes("jd-strip-title")
                range_label = ui.label().classes("jd-meta")
                # Rebuilt rather than re-texted: each part is a control now.
                hidden_host = ui.row().classes("items-center gap-2 jd-piles")
                # The chip belongs at the top of the page, not beside the
                # filters: an update notice under fifty rows is an update
                # notice nobody sees.
                chip_host = ui.row().classes("items-center ml-auto gap-2")
                with chip_host:
                    ui.label("j k bewegen · x Firma ausblenden · s merken · "
                             "o Anzeige öffnen · ⏎ Hauptaktion") \
                        .classes("jd-meta")
            with ui.element("div").classes("jd-panes"):
                with ui.element("div").classes("jd-list"):
                    with ui.row().classes("items-center gap-3 px-2 pt-2 w-full"):
                        search_button = ui.button(
                            "Jetzt suchen", icon="search",
                            on_click=lambda: search_now()) \
                            .props("flat dense").mark("poll-now")
                        poll_label = ui.label("").classes("jd-meta") \
                            .mark("poll-line")
                    with ui.row().classes("items-center gap-2 p-2"):
                        # `debounce` is Quasar's own: without it every
                        # keystroke resets the page, drops the selection and
                        # awaits a full reload — three windowed CTEs, four pile
                        # counts, a fifty-row rebuild and a markdown re-render,
                        # ten times over for "Entwickler".
                        search_box = ui.input(placeholder="suchen …") \
                            .props("dense outlined clearable debounce=350") \
                            .classes("flex-1")
                        search_box.on_value_change(
                            lambda e: set_search(e.value or ""))
                        ui.select(
                            {view.key: view.label for view in VIEWS
                             if view.key in MAIN_VIEW_KEYS},
                            value=DEFAULT_VIEW,
                            label="Ansicht",
                            on_change=lambda e: set_view(e.value),
                        ).mark("view-select").props("dense outlined") \
                            .classes("min-w-36")
                        ui.select(
                            SCORE_FLOORS, value=DEFAULT_MIN_SCORE,
                            label="Ab Bewertung",
                            on_change=lambda e: set_min_score(e.value),
                        ).mark("score-select").props("dense outlined") \
                            .classes("min-w-32")
                        ui.select(
                            SORT_LABELS, value=db.DEFAULT_LIST_ORDER,
                            label="Sortierung",
                            on_change=lambda e: set_sort(e.value),
                        ).mark("sort-select").props("dense outlined") \
                            .classes("min-w-36")
                    # A sibling of the rows and ABOVE the scroll container, so
                    # an application under way is on screen in every view, at
                    # every page, under any search. `.jd-list` is a flex
                    # column and `.jd-rows` carries the overflow, so this is
                    # pinned structurally — no sticky positioning, no change
                    # to the grid.
                    laeuft_host = ui.column().classes("jd-laeuft w-full gap-0")
                    rows_host = ui.column().classes("jd-rows w-full gap-0") \
                        .props('role=listbox aria-label="Anzeigen"')
                    pager = ui.row().classes("items-center gap-2 p-2")
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
                tuple(read_here), state["min_score"], state["sort"])
            if gen != refresh_gen["n"] or view is None:
                return  # superseded, or the page is going away
            state["page"] = view["page"]   # the loader clamped it to what exists
            poll_label.set_text(poll_line(view["poll"]))
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
            fingerprints = {row["id"]: _row_fingerprint(row) for row in view["rows"]}
            list_state = (new_order, list(fingerprints.values()), selected,
                          view["total"], view["page"],
                          [(job_id, siblings_key(view, job_id))
                           for job_id in new_order])
            reader_state = (selected, fingerprints.get(selected),
                            _row_fingerprint(dropped) if dropped else None,
                            view["applied"].get(selected))
            # Two comparisons, not one: the daily liveness pass re-stamps
            # hundreds of rows in a couple of minutes, and every one of those
            # ticks was rebuilding the advert he is reading along with the list
            # it belongs to.
            # A THIRD comparison: the strip is not part of either pane, and
            # it changes on writes neither of them cares about — a Mappe
            # landing in the background is the whole reason it self-updates.
            strip_state = [(r["id"], r["form_opened_at"], r["mappe_kind"],
                            r["draft_status"], r["company"])
                           for r in view["started"]]
            list_same = not force and list_state == drawn.get("list")
            reader_same = not force and reader_state == drawn.get("reader")
            strip_same = not force and strip_state == drawn.get("strip")
            drawn["list"], drawn["reader"] = list_state, reader_state
            drawn["strip"] = strip_state

            strip_rows.clear()
            strip_rows.update({r["id"]: r for r in view["started"]})
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
            range_label.set_text(_range_line(
                view["page"], view["total"], len(view["rows"])))
            render_piles(view)
            if not strip_same:
                render_strip(view)
            if not list_same:
                render_rows(view)
                render_pager(view)
            if not reader_same:
                render_reader(gone=dropped is not None)

        def siblings_key(view: dict, job_id: int):
            """What the row's sibling note and the reader's sibling list draw."""
            row = next((r for r in view["rows"] if r["id"] == job_id), None)
            if row is None:
                return ()
            group = view["siblings"].get(row.get("company_key"), [])
            return (row.get("company_count"),
                    tuple((s["id"], s["title"], s["effective_score"])
                          for s in group))

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

        def render_strip(view: dict) -> None:
            """Every application under way, above the list, in every view.

            Rebuilt whole, and `age_labels` is repopulated here — the beat
            below holds element references, and a `set_text` on an element some
            other redraw already deleted raises inside a timer callback, which
            is one log line and a strip frozen at whatever it last said."""
            laeuft_host.clear()
            age_labels.clear()
            started = view["started"]
            if not started:
                return
            with laeuft_host:
                for job in started:
                    _render_started(job)

        def _render_started(job: dict) -> None:
            with ui.row().classes("jd-laeuft-row items-center gap-3 w-full"):
                age_labels[job["id"]] = ui.label(started_line(job)) \
                    .classes("text-sm")
                text, kind = mappe_line(job)
                ui.label(text).classes(f"jd-meta {kind}")
                if job["upload_path"]:
                    # The one thing an upload dialog actually asks for, on
                    # screen. "Mappe bereit" is true and useless at the moment
                    # the form says "Datei auswählen": the file manager we open
                    # lands BEHIND the employer's tab, and the path was
                    # reachable only through a menu he had no reason to open.
                    _mappe_path_control(job["upload_path"])
                ui.space()
                ui.button("Abgeschickt",
                          on_click=lambda j=job: confirm_applied(j)) \
                    .props("dense no-caps color=positive")
                with ui.button(icon="more_horiz").props("flat dense"):
                    with ui.menu():
                        _started_menu(job)

        def _mappe_path_control(path: str) -> None:
            """The file name, and one press that puts the full path on the
            clipboard — Ctrl+L, Ctrl+V is the way into any file dialog.

            A button rather than an automatic copy: the Mappe lands about a
            minute after his press, and a browser refuses a clipboard write
            that far from a user gesture."""
            name = pathlib.Path(path).name
            ui.button(f"⧉ {name}",
                      on_click=lambda p=path: (
                          ui.clipboard.write(p),
                          say("Pfad kopiert — im Datei-Dialog Strg+L, Strg+V.",
                              type="positive"))) \
                .props("flat dense no-caps").classes("jd-chip")

        def _started_menu(job: dict) -> None:
            if job["upload_path"]:
                ui.menu_item("Ordner öffnen",
                             lambda: helpers.open_in_system(
                                 str(config.UPLOAD_DIR)))
            url = _openable_url(job)
            if url:
                ui.menu_item("Formular erneut öffnen",
                             lambda u=url: ui.navigate.to(u, new_tab=True))
            ui.menu_item("Formulardaten",
                         lambda j=job: show_form_data(j))
            ui.menu_item("Doch nicht beworben",
                         lambda j=job["id"]: revive(j))

        async def show_form_data(job: dict) -> None:
            """What a German form asks for, ready to copy.

            This is what is left of the cockpit, and it is deliberately less:
            the eleven personal values are chips because they are the same on
            every form, while the six the browser cannot know keep rows of
            their own. The trade is one alt-tab back to JobDeck against a whole
            second screen to keep in sync.
            """
            overlay.clear()
            view = await run.io_bound(_form_data, job["id"])
            if view is None:
                say("Diese Anzeige gibt es nicht mehr.", type="warning")
                return
            with overlay, ui.dialog() as sheet, \
                    ui.card().classes("w-[640px] max-w-full"):
                ui.label(f"Formulardaten — {view['company']}") \
                    .classes("font-bold")
                with ui.row().classes("gap-1 flex-wrap my-2"):
                    for field in view["personal"]:
                        _copy_chip(field)
                with ui.element("div").classes("jd-facts w-full"):
                    for field in view["posting"]:
                        ui.label(field.label).classes("k")
                        _copy_value(field)
                with ui.row().classes("justify-end w-full"):
                    ui.button("Schließen", on_click=sheet.close) \
                        .props("flat no-caps")
            sheet.open()

        def _copy_chip(field) -> None:
            ui.button(f"{field.label}: {_short(field.value)}",
                      on_click=lambda v=field.value: ui.clipboard.write(v)) \
                .props("flat dense no-caps").classes("jd-chip")

        def _copy_value(field) -> None:
            if not field.ready:
                ui.label(field.hint or "—").classes("jd-note warn")
                return
            ui.button(_short(field.value),
                      on_click=lambda v=field.value: ui.clipboard.write(v)) \
                .props("flat dense no-caps align=left")

        def restamp_strip() -> None:
            """Let the running applications get older, without redrawing.

            `set_text` only, never a rebuild: this fires on every tick of the
            page's watcher, including while he has a dialog open or is reading
            an advert, and a rebuild then would take both away from him. It is
            also why the ten-minute question is a LABEL — there is nothing to
            dismiss, so there is nothing to learn to dismiss."""
            for job_id, label in list(age_labels.items()):
                job = strip_rows.get(job_id)
                if job is not None:
                    label.set_text(started_line(job))

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
                # `row_meta` already states the age; `_score_line` would state
                # it a second time in the same line ("61 T · … · 61 Tage alt").
                ui.label(row_meta(job) + _score_line(job, with_age=False)) \
                    .classes("jd-meta mt-2")
                # Triage first: the three things he does WITHOUT reading, so
                # they are reachable before the text and again from the
                # keyboard. The one action that commits him waits below it.
                with ui.row().classes("gap-2 my-4 flex-wrap"):
                    marked = bool(job["bookmarked_at"])
                    ui.button("★ gemerkt" if marked else "☆ merken",
                              on_click=lambda j=job["id"]: toggle_bookmark(j)) \
                        .props("flat dense no-caps")
                    if job.get("company_hidden"):
                        # In the hidden view the triage buttons are about a
                        # posting, and the decision here was about a company —
                        # so this is the one that undoes what he actually did.
                        ui.button("↩ Firma wieder anzeigen",
                                  on_click=lambda c=job["company"]:
                                      unhide(c)) \
                            .props("flat dense").mark("unhide-company")
                    elif job["status"] == "skipped" or job["form_opened_at"]:
                        # Two different ways back, one button: he put the
                        # posting away, or he started a form here and decided
                        # against it. The second is the one that needs asking —
                        # `revive` does that.
                        ui.button("↩ zurück in die Arbeitsliste",
                                  on_click=lambda j=job["id"]: revive(j)) \
                            .props("flat dense no-caps")
                    else:
                        ui.button("✕ Firma ausblenden",
                                  on_click=lambda j=job["id"]:
                                      not_interested(j)) \
                            .props("flat dense").mark("job-x") \
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
                    if job["status"] == "new":
                        # Also the correction path, so it stays offered once an
                        # address exists: the one the lookup found can be the
                        # wrong inbox, and only he can see what the posting
                        # actually says.
                        # NOT "eintragen": that verb belongs to the apply
                        # steps, and a test sweeps every button carrying it
                        # into the set that must go dead at a company already
                        # applied to. Recording an address is harmless there.
                        ui.button("Kontaktdaten hinterlegen"
                                  if not job["contact_email"]
                                  else "Kontaktdaten",
                                  on_click=lambda j=job: enter_contact(j)) \
                            .props("flat dense no-caps") \
                            .mark("enter-contact")
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
            """Every act this posting needs, on the screen it is read from.

            The whole toolkit rather than one polymorphic button: an
            application is three or four acts, and hiding the rest behind
            other screens is what made recording a form application a detour."""
            steps = apply_steps(job, already)
            following = _next_step(steps)
            with ui.row().classes("items-start gap-3 mt-6 flex-wrap"):
                for index, step in enumerate(steps):
                    # Each reason sits UNDER its own button rather than in a
                    # list below them all: repeating the label to say which
                    # step a line belongs to put the same words on screen
                    # twice, and a tooltip cannot be reached from the keyboard.
                    with ui.column().classes("gap-1 items-start max-w-64"):
                        button = ui.button(
                            ("✓ " if step.done else "") + step.label)
                        button.props("no-caps" + (
                            "" if index == following else " outline"))
                        button.set_enabled(step.enabled)
                        if step.enabled:
                            button.on_click(
                                lambda _=None, j=job, k=step.key, b=button:
                                    run_step(k, j, b))
                        if step.reason and not step.done:
                            ui.label(step.reason).classes("jd-reason")


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

        def render_piles(view: dict) -> None:
            """The piles, as doors. Every number under the list opens the view
            that holds what it counts — they were printed and never clickable,
            so a pile was named on screen and reachable only through a dropdown
            that named it again."""
            hidden_host.clear()
            parts = hidden_parts(view["view"], view["counts"],
                                 view["stale_age_days"], search=view["search"])
            with hidden_host:
                for index, part in enumerate(parts):
                    if index:
                        ui.label("·").classes("jd-meta")
                    if part["view"] is None:
                        ui.label(part["text"]).classes("jd-meta")
                        continue
                    ui.button(part["text"],
                              on_click=lambda k=part["view"]: set_view(k)) \
                        .props("flat dense").classes("jd-pile-door") \
                        .mark(f"pile-{part['view']}")
                if parts:
                    # Said once, at the end, instead of "ausgeblendet" after
                    # every number — five of them in one line is the noise he
                    # rejected the arrangement over.
                    ui.label("Nichts wird gelöscht.").classes("jd-meta")

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

        async def set_min_score(value) -> None:
            """The floor under the list. Three quarters of what he scrolls is
            noise the machine has already graded as weak."""
            state["min_score"] = _whole(value)
            state["page"] = 0    # a shorter list: page 3 of it means nothing
            state["selected"] = None
            await refresh(force=True)

        async def set_sort(value) -> None:
            state["sort"] = value or db.DEFAULT_LIST_ORDER
            state["page"] = 0
            state["selected"] = None
            await refresh(force=True)

        async def search_now() -> None:
            """Look for postings, now, from the screen he is looking at.

            The function is the one Einstellungen used to call in English. It
            is a coroutine and is awaited directly — `run.io_bound` would hand
            a worker thread an un-awaited coroutine, and the arity rule that
            catches the shape cannot see a mistake this asks for.
            """
            if polling.running():
                say("Es läuft schon eine Suche.", type="warning")
                return
            search_button.disable()
            search_button.set_text("Sucht …")
            try:
                await polling.poll_all_profiles(force=True)
            except Exception as exc:        # a source can refuse at any moment
                log.exception("manual poll failed")
                say(f"Die Suche ist gescheitert: {exc}", type="warning",
                    multi_line=True)
            finally:
                search_button.set_text("Jetzt suchen")
                search_button.enable()
            await refresh(force=True)

        async def ask_before_committing(step: Step, job: dict) -> bool:
            """The keyboard asks before an action he cannot take back cheaply.

            ⏎ is the one control on this screen whose meaning changes with the
            row under the cursor: he moves with j and presses it expecting
            "open this". A button he CLICKED carries its own label — that is
            why "Bewerbung starten · ~0,09 $" needs no second question — but ⏎
            carries nothing, so it asks.

            Two kinds of press qualify, and the second is the one this screen
            got wrong. Money: a Sonnet call of about nine cents and a minute.
            And the LEDGER: once a form is started, the next step under ⏎ is
            "Abgeschickt", which writes into the very table the duplicate gate
            reads — one keystroke spending that company's only application
            slot. The button has ten seconds of "Rückgängig" beside it; a
            keystroke he did not mean to make has nobody watching for it.

            One dialog, built once: the branches are exclusive but the rule
            that keeps `overlay.clear()` ahead of every await cannot know that,
            and it is right not to guess.
            """
            if step.key == STEP_RECORD:
                heading = "Bewerbung eintragen?"
                detail = ("Eine Bewerbung pro Firma — danach ist diese Firma "
                          "vergeben.")
                go = "Eintragen"
            elif step.key in (STEP_DRAFT, STEP_START):
                heading = (f"Bewerbung schreiben? "
                           f"~{_usd(preparing.COST_PER_DRAFT_USD)}")
                detail = ("Das schreibt der KI-Dienst, kostet Geld und dauert "
                          "etwa eine Minute.")
                go = "Schreiben"
            else:
                return True
            overlay.clear()
            with overlay, ui.dialog() as confirm, ui.card():
                ui.label(heading).classes("font-bold")
                ui.label(f"{job['company']} — {clean_title(job['title'])}") \
                    .classes("text-sm")
                ui.label(detail).classes("text-sm text-gray-600")
                with ui.row().classes("justify-end gap-2 w-full"):
                    ui.button("Abbrechen",
                              on_click=lambda: confirm.submit(False)) \
                        .props("flat no-caps")
                    ui.button(go, on_click=lambda: confirm.submit(True)) \
                        .props("no-caps")
            confirm.open()
            return bool(await confirm)

        async def run_step(key: str, job: dict, button) -> None:
            """Do one act of applying, from the screen he read the posting on."""
            if key == STEP_DRAFT:
                await draft(job, force=True, button=button)
            elif key == STEP_SEND:
                await open_draft_editor(job)
            elif key == STEP_START:
                await start_application(job, button)
            elif key == STEP_RECORD:
                await confirm_applied(job)

        async def build_mappe(job: dict, button) -> None:
            """The PDF he uploads to an employer's form."""
            # Cleared BEFORE the wait, never after: the page stays live
            # while this runs, and clearing afterwards destroys a dialog he
            # opened meanwhile — one awaited on then never resolves at all.
            overlay.clear()
            say("Bewerbungsmappe wird gebaut…")
            if button is not None:
                button.disable()
            result = await mappe.create_mappe(job["id"])
            await refresh(force=True)
            with overlay:
                if not result["ok"]:
                    say(result["error"], type="warning", multi_line=True)
                    return
                say(helpers.mappe_summary(result, with_anlagen=True),
                    type="positive", multi_line=True)
                if result["warning"]:
                    say(result["warning"], type="warning", multi_line=True)

        async def open_draft_editor(job: dict) -> None:
            """The one editor — the same the review queue opens."""
            # Cleared BEFORE the wait, never after: the page stays live
            # while this runs, and clearing afterwards destroys a dialog he
            # opened meanwhile — one awaited on then never resolves at all.
            overlay.clear()
            row = await run.io_bound(draft_editor.load, job["id"])
            if row is None:
                say("Kein Entwurf zu dieser Anzeige.", type="warning")
                return
            with overlay:
                draft_editor.open_editor(
                    row, overlay=overlay, say=say, on_change=forced_refresh)

        async def forced_refresh() -> None:
            await refresh(force=True)

        async def start_application(job: dict, button=None) -> None:
            """One press: his tab opens, and everything behind it is prepared.

            The ORDER is the feature, and the first two steps are forced by the
            browser rather than chosen:

            1. the overlay is cleared before anything is awaited — clearing it
               afterwards destroys a dialog he opened meanwhile, and one
               awaited on then never resolves at all;
            2. the employer's page is opened BEFORE the first await. A
               window.open pushed after a minute of server work is what a popup
               blocker refuses, and the letter takes about a minute.

            Then, while he reads their form: the moment is stamped, the channel
            is resolved if the scheduler has not got to it yet, the letter is
            written, the COMPLETE Mappe is built and staged where their upload
            dialog opens, and that folder is opened for him.

            If the letter fails the Mappe is not built and the strip says the
            documents are NOT complete. A Bewerbungsmappe is always complete by
            his decision, so a partial one put silently in front of an upload
            button is the worst outcome available here.
            """
            overlay.clear()
            url = _openable_url(job)
            if not url:
                say("Diese Anzeige nennt keine Adresse zum Öffnen.",
                    type="warning")
                return
            ui.navigate.to(url, new_tab=True)
            if button is not None:
                button.disable()
            await run.io_bound(_mark_form_opened, job["id"])
            await refresh(force=True)
            # Resolved after the tab is open rather than before it: the two
            # cannot both come first, and a tab that opens on the board's own
            # page is what he would have clicked through anyway. The strip's
            # "Formular erneut öffnen" carries the resolved link.
            if not job.get("apply_channel"):
                try:
                    await apply_resolve.resolve_and_store(job["id"])
                except Exception as exc:      # noqa: BLE001 — never block the press
                    log.warning("channel resolution failed for job %s: %s",
                                job["id"], exc)
            result = await drafting.draft_for_job(job["id"])
            if not result["ok"]:
                await refresh(force=True)
                with overlay:
                    say(f"Kein Anschreiben — die Mappe ist NICHT vollständig: "
                        f"{result['error']}", type="warning", multi_line=True)
                return
            built = await mappe.create_mappe(job["id"])
            await refresh(force=True)
            with overlay:
                if not built["ok"]:
                    say(f"Die Mappe ist NICHT vollständig: {built['error']}",
                        type="warning", multi_line=True)
                    return
                say(helpers.mappe_summary(built, with_anlagen=True),
                    type="positive", multi_line=True)
                staged = await run.io_bound(_staged_name, job["id"])
                if staged:
                    # Where it IS, not merely that it exists. The folder we
                    # open for him appears behind the employer's tab, so the
                    # notification has to carry the answer too.
                    say(f"Zum Hochladen bereit: {staged}\n"
                        f"Ordner: {config.UPLOAD_DIR}",
                        type="positive", multi_line=True)
                if built["warning"]:
                    # The portal budget is 2 MB and his real Mappe measures
                    # about 2.1: the step this press replaced said so, and an
                    # oversized upload fails in front of the employer.
                    say(built["warning"], type="warning", multi_line=True)
            await run.io_bound(_open_upload_folder)

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
                if job["form_opened_at"]:
                    # `x` used to be a no-op here, for free: a started form had
                    # left status 'new'. Now it has not, so one keystroke would
                    # move it to 'skipped' — off the strip, and the strip is
                    # the app's ONLY record that an application may already be
                    # out at that company. It asks instead, exactly as the
                    # row's own "zurück in die Arbeitsliste" does.
                    await revive(job["id"])
                else:
                    await not_interested(job["id"])
            elif key == "s":
                await toggle_bookmark(job["id"])
            elif key == "o":
                url = _openable_url(job)
                if url:
                    with overlay:
                        ui.navigate.to(url, new_tab=True)
            elif key == "Enter":
                # The very step the solid button runs. Two state machines on
                # one screen agree only by parallel construction, and this one
                # can reach a send.
                steps = apply_steps(job, applied.get(job["id"]))
                index = _next_step(steps)
                if index >= 0 and await ask_before_committing(steps[index], job):
                    await run_step(steps[index].key, job, None)

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
            """Put a posting back into the working list.

            From a started form it asks first, and the question is not "are you
            sure": `form_opened_at` is the app's ONLY record that an
            application at that company may already be out, and an unrecorded
            form submission is invisible to every gate there is. Pressing this
            after actually applying would erase the one hint and let a second
            application through."""
            job = shown.get(job_id)
            if job is None or not (job["status"] == "skipped"
                                   or job["form_opened_at"]):
                return
            if job["form_opened_at"]:
                overlay.clear()
                with overlay, ui.dialog() as ask, ui.card():
                    ui.label("Du hattest das Formular geöffnet.") \
                        .classes("font-bold")
                    ui.label(f"{job['company']} — hast du dich beworben?") \
                        .classes("text-sm")
                    with ui.row().classes("justify-end gap-2 w-full"):
                        ui.button("Nein, zurück in die Liste",
                                  on_click=lambda: ask.submit("back")) \
                            .props("flat no-caps")
                        ui.button("Ja, eintragen",
                                  on_click=lambda: ask.submit("record")) \
                            .props("color=positive no-caps")
                ask.open()
                answer = await ask
                if answer == "record":
                    await confirm_applied(job)
                    return
                if answer != "back":
                    return
                _hold_place(job_id)
                # He says no application went out: take back the start, and
                # with it whatever was staged for the upload dialog.
                await run.io_bound(_clear_form_opened, job_id)
                await refresh(force=True)
                return
            _hold_place(job_id)
            await run.io_bound(_set_status, job_id, "new")
            await refresh(force=True)

        async def not_interested(job_id: int) -> None:
            """`x` — and it now reaches the COMPANY, not this one advert.

            Measured before the change: eleven presses, eleven different
            companies. Per-posting exclusion had never once helped him, while
            nineteen companies hold a hundred and fifty of his open postings
            and one staffing agency holds seven under seven different branch
            names. Hiding the advert in front of him left the other six.

            A posting with no company keeps the old behaviour: a blank field is
            missing information, not an employer, so there is nothing to hide
            but this row.
            """
            job = shown.get(job_id)
            if job is None or job["status"] != "new":
                return
            if job["form_opened_at"]:
                # Defence in depth: `revive` is the way out of a started
                # application because it ASKS whether one went out. Reaching
                # this instead would silently delete the only hint there is.
                await revive(job_id)
                return
            _hold_place(job_id)
            result = await run.io_bound(_hide_company, job["company"] or "")
            if not result["ok"]:
                await run.io_bound(_set_status, job_id, "skipped")
                await refresh(force=True)
                return
            await refresh(force=True)
            with overlay:
                _hidden_bar(result)

        async def unhide(company: str) -> None:
            await run.io_bound(_unhide_company, norm(company or ""))
            await refresh(force=True)
            say(f"{company} ist wieder in der Liste.", type="positive")

        def _hidden_bar(result: dict) -> None:
            """Ten seconds in which the press can be taken back, with the price
            written into it.

            No confirmation dialog: he hides companies often, and a dialog on
            every press is a dialog he learns to click through, which guards
            nothing. What makes the trade honest is that the undo is real work
            and the bar states what was hidden — including that it goes on
            applying to searches he has not run yet.
            """
            bar = ui.row().classes("jd-undo items-center gap-3")
            gone = {"yes": False}

            async def dismiss() -> None:
                if gone["yes"] or bar.is_deleted:
                    gone["yes"] = True
                    return
                gone["yes"] = True
                bar.delete()

            with bar:
                best = (f", die beste mit Bewertung {result['best']}"
                        if result["best"] else "")
                many = result["jobs"] != 1
                ui.label(
                    f"{result['company']} ausgeblendet — "
                    f"{result['jobs']} {'Anzeigen' if many else 'Anzeige'}"
                    f"{best}. Gilt auch für neue Suchen.").classes("text-sm")

                async def take_back() -> None:
                    await dismiss()
                    await run.io_bound(_unhide_company, result["key"])
                    await refresh(force=True)
                    say(f"{result['company']} ist wieder in der Liste.",
                        type="positive")

                ui.button("Rückgängig", on_click=take_back) \
                    .props("flat dense").mark("undo-hide")
            with timers:
                ui.timer(10.0, dismiss, once=True)

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
                    f"im Postausgang"
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
                    ui.button("Postausgang", icon="outbox",
                              on_click=lambda: ui.navigate.to(rail.POSTAUSGANG_PATH)) \
                        .props("outline no-caps")
                    ui.button("Schließen", on_click=dialog.close) \
                        .props("flat no-caps")
            dialog.open()

        async def redraft(dialog, job: dict):
            dialog.close()
            await draft(job, force=True)

        async def draft(job: dict, force: bool = False, button=None):
            # Cleared BEFORE the wait, never after: the page stays live
            # while this runs, and clearing afterwards destroys a dialog he
            # opened meanwhile — one awaited on then never resolves at all.
            overlay.clear()
            # a finished draft costs nothing to show again — regenerate only
            # on explicit request
            if not force:
                existing = await run.io_bound(_load_draft, job["id"])
                if existing is not None and existing["status"] == "ready":
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
            # Cleared BEFORE the wait, never after: the page stays live
            # while this runs, and clearing afterwards destroys a dialog he
            # opened meanwhile — one awaited on then never resolves at all.
            overlay.clear()
            say("Kontakt-E-Mail wird gesucht…")
            res = await contact_lookup.lookup_and_propose(job["id"])
            if not res["email"]:
                say("Keine verifizierte Bewerbungs-E-Mail gefunden",
                    type="warning")
                return
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

        async def enter_contact(job: dict):
            """Type in what the posting only shows a human.

            The Arbeitsagentur hides an employer's address behind a CAPTCHA
            this app must never solve, so on those postings he is the only
            reader who can ever see it. Writing an address here also settles
            the channel: the posting stops being a form job and the ordinary
            e-mail path opens."""
            # Cleared BEFORE the wait, never after: clearing afterwards
            # destroys a dialog opened meanwhile, and an awaited one then
            # never resolves. Same rule as find_email above.
            overlay.clear()
            stored = await run.io_bound(_contact_values, job["id"])
            with overlay, ui.dialog() as dialog, \
                    ui.card().classes("w-[460px] max-w-full"):
                ui.label("Kontaktdaten hinterlegen").classes("font-bold")
                ui.label(job["company"]).classes("text-sm text-gray-600")
                fields = {}
                for key, label in CONTACT_LABELS:
                    fields[key] = ui.input(label, value=stored[key]) \
                        .classes("w-full").props("dense")
                ui.label("Die E-Mail macht aus der Anzeige eine Bewerbung per "
                         "E-Mail. Ansprechpartner und Adresse stehen im "
                         "Briefkopf und in der Anrede.") \
                    .classes("text-xs text-gray-500")

                async def save() -> None:
                    values = {k: f.value or "" for k, f in fields.items()}
                    dialog.close()
                    await run.io_bound(_save_contact_values, job["id"], values)
                    say("Kontaktdaten gespeichert", type="positive")
                    await refresh(force=True)

                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Speichern", icon="check", on_click=save) \
                        .props("color=positive no-caps").mark("save-contact")
                    ui.button("Abbrechen", on_click=dialog.close) \
                        .props("flat no-caps")
            dialog.open()

        async def confirm_applied(job: dict):
            """Record the application, and let him take it straight back.

            This used to ask first, because it is a one-way door: `apply_job`
            writes into the very table the duplicate gate reads, so a mis-click
            burns that company's single application slot. The question is gone
            and an undo took its place — a dialog on the press he makes eight
            times an evening is a dialog he learns to click through, which
            guards nothing. The undo is real work (`db.unrecord_application`
            takes back every write, in one transaction), and that is the only
            reason the trade is honest."""
            # Cleared BEFORE the wait, never after: clearing afterwards
            # destroys a dialog he opened meanwhile, and one awaited on then
            # never resolves at all.
            overlay.clear()
            # What the ledger records is the way it actually went — the
            # duplicate gate reads this table, so its rows have to be true.
            kanal = (apply_record.KANAL_EMAIL if _by_email(job)
                     else apply_record.KANAL_FORM)
            # He asked for it to go, so it goes: without this the posting would
            # be treated as one that fell out of the view by itself and stay in
            # the reading pane behind a note.
            _hold_place(job["id"])
            result = await run.io_bound(_record_application, job["id"], kanal)
            await refresh(force=True)
            if not result["ok"]:
                blocking = result["duplicate"]
                say(helpers.applied_line(blocking) if blocking else
                    "Diese Anzeige gibt es nicht mehr.",
                    type="warning", multi_line=True)
                return
            with overlay:
                _undo_bar(result, job)

        def _undo_bar(result: dict, job: dict) -> None:
            """Ten seconds in which the press can be taken back.

            Built as real elements rather than a Quasar notify action: nothing
            in this codebase uses those, an unwired handler renders an
            identical button, and a test that called the service directly would
            pass while the button did nothing."""
            bar = ui.row().classes("jd-undo items-center gap-3")
            gone = {"yes": False}

            async def dismiss() -> None:
                """Idempotent, because two things end this bar and either can
                come first: the ten seconds running out, and him pressing.
                Deleting an already-deleted element raises, and inside a timer
                callback that is one log line — the failure would be invisible
                and the next press would find a page carrying a dead timer."""
                if gone["yes"] or bar.is_deleted:
                    gone["yes"] = True
                    return
                gone["yes"] = True
                bar.delete()

            with bar:
                ui.label(f"Bewerbung bei {result['company']} eingetragen ✓") \
                    .classes("text-sm")

                async def take_back() -> None:
                    await dismiss()
                    await run.io_bound(_undo_application, job["id"],
                                       result["bewerbung_id"],
                                       result["previous_status"])
                    _hold_place(job["id"])
                    await refresh(force=True)
                    say(f"Rückgängig gemacht — {result['company']} ist wieder "
                        f"frei.", type="positive")

                ui.button("Rückgängig", on_click=take_back) \
                    .props("flat dense no-caps")
            # once=True: a repeating timer in a page module is refused by the
            # rule that keeps every recurring tick in ui/live.py, where the
            # pause-on-disconnect lives. Parented outside `overlay`, which the
            # next handler clears — see `timers` above.
            with timers:
                ui.timer(10.0, dismiss, once=True)

        # Postings arrive hourly, scores land every ten minutes and the liveness
        # pass runs 90 s after every start — all of it invisible until this. It
        # rebuilds only when the data really changed, and never while a dialog
        # is on screen: the reading pane survives a rebuild by itself, so unlike
        # the old expansions there is nothing here to yank out from under him.
        with chip_host:
            live_view = live.watch(_signature, refresh, busy=live.dialog_open,
                                   beat=restamp_strip)
        # `ignore` is NiceGUI's own client-side rule and the only one that can
        # work: a keystroke typed into the search box must never reach here, and
        # only the browser knows where the caret is. Pinned by a test, because
        # 'x' landing on the page while he types a company name would throw the
        # posting away.
        ui.keyboard(on_key=on_key, ignore=["input", "select", "button", "textarea"])
        await refresh(force=True)
