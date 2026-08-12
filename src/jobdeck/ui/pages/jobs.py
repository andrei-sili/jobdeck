"""Job inbox: discovered postings with per-job actions."""

import logging
import math
import pathlib
from dataclasses import dataclass

from fastapi.responses import RedirectResponse
from nicegui import app, run, ui

from jobdeck import apply_channel, db, freshness
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


def _hidden_line(view: View, counts: dict, stale_age_days: int) -> str:
    """What this view is not showing, derived from the filters it actually used
    so the label cannot contradict the list. Independent statements, never a
    total: a posting can be a mismatch AND offline AND old."""
    parts = []
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


def _load_jobs(view_key: str, page: int) -> dict:
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
        filters = {**view.filters, "stale_age_days": stale_age_days}
        total = db.count_job_groups(con, view.status, **filters)
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
            },
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


def _claim_is_running(job: dict) -> bool:
    """Is a draft for this posting being written RIGHT NOW?

    Only then may the Draft button hide. `drafting._claim` re-claims a row
    abandoned past CLAIM_TIMEOUT_MIN, and this button is the only surface
    that can trigger it — hiding it for as long as the row merely says
    'generating' left a crashed draft with no way back."""
    return (job["draft_status"] == "generating"
            and not drafting.claim_is_stale(job["draft_updated_at"]))


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
    async with frame("Stellen", current="stellen"):
        current = {"view": DEFAULT_VIEW}
        # What the duplicate gate says about the rows currently on screen,
        # refreshed with them. Not `jobs.duplicate_of`: that is written once at
        # discovery, so every application he sends makes more rows stale.
        applied: dict[int, dict] = {}
        page = {"value": 0}
        refresh_gen = {"n": 0}  # rapid filter/switch flips: last request wins
        # How many postings he has expanded to read. A rebuild collapses every
        # one of them, so while this is above zero the live watcher defers.
        reading = {"rows": 0}
        # First element of the page: an update notice under fifty rows is an
        # update notice nobody sees. The filters live at the bottom (they always
        # have), so this cannot ride along with them.
        header = ui.row().classes("w-full items-center gap-2")
        container = ui.column().classes("w-full gap-2")
        pager = ui.row().classes("items-center gap-2")
        # Feedback has to outlive the row that asked for it. A handler runs in
        # the slot of the button that fired it, and refresh() clears
        # `container` — so a dialog or notification built after a refresh has
        # no live parent and raises instead of appearing, silently swallowing
        # the very error it was meant to report. This host is a sibling of the
        # list, so no refresh can delete it; clearing it first keeps exactly
        # one dialog alive at a time.
        overlay = ui.column().classes("contents")

        def say(message: str, **kwargs) -> None:
            """Tell the user something, from a slot no refresh can delete.

            `ui.notify` needs a live parent, and a handler's own is the element
            that fired it — which this page's refresh (and, in the queue, its
            timer) removes. Every message goes through here so the question
            "is my slot still alive?" never has to be asked at a call site."""
            with overlay:
                ui.notify(message, **kwargs)

        async def refresh():
            refresh_gen["n"] += 1
            gen = refresh_gen["n"]
            view = await run.io_bound(_load_jobs, current["view"], page["value"])
            if gen != refresh_gen["n"]:
                return  # superseded — a newer refresh already owns the view
            page["value"] = view["page"]  # the loader clamped it to what exists
            container.clear()
            reading["rows"] = 0  # every expansion just died with the container
            live_view.mark(view["signature"])
            hidden_label.set_text(_hidden_line(
                view["view"], view["counts"], view["stale_age_days"]))
            with container:
                if not view["rows"]:
                    ui.label(view["view"].empty).classes("text-gray-500")
                applied.clear()
                applied.update(view["applied"])
                for job in view["rows"]:
                    render_job(job, view["siblings"].get(job.get("company_key"), []))
            render_pager(view)

        def render_pager(view: dict):
            pager.clear()
            with pager:
                ui.label(_range_line(view["page"], view["total"],
                                     len(view["rows"]))) \
                    .classes("text-xs text-gray-500")
                if view["pages"] <= 1:
                    return
                ui.button(icon="chevron_left", on_click=lambda: turn_page(-1)) \
                    .props("flat dense").set_enabled(view["page"] > 0)
                ui.label(f"Seite {view['page'] + 1}/{view['pages']}") \
                    .classes("text-xs text-gray-500")
                ui.button(icon="chevron_right", on_click=lambda: turn_page(1)) \
                    .props("flat dense") \
                    .set_enabled(view["page"] + 1 < view["pages"])

        async def turn_page(step: int):
            page["value"] += step
            await refresh()

        def track_reading(event) -> None:
            """Count the postings he currently has open."""
            reading["rows"] = max(0, reading["rows"] + (1 if event.value else -1))

        def render_job(job: dict, siblings: list[dict] = ()):
            remote = " · remote" if job["remote"] else ""
            head = (f"{job['title']}  —  {job['company']}"
                    f" ({job['location'] or 'n/a'}{remote}{_score_line(job)})")
            others = job.get("company_count", 1) - 1
            if others > 0:
                head += f"  +{others}"
            # An open row is him READING a posting: the live watcher must not
            # pull the list out from under it (see ui/live.py).
            with ui.expansion(head, on_value_change=track_reading) \
                    .classes("w-full border rounded"):
                ui.label(f"Source: {job['source']} · found {job['fetched_at'][:16]} · "
                         f"status: {job['status']}").classes("text-xs text-gray-500")
                if job["match_reason"]:
                    mismatch = job["match_score"] == 0
                    ui.label(f"Match: {job['match_reason']}").classes(
                        "text-sm text-red-700" if mismatch else "text-sm text-gray-600"
                    )
                if job["contact_email"]:
                    ui.label(f"Contact: {job['contact_email']}").classes("text-sm")
                salary = _salary_line(job)
                if salary:
                    ui.label(salary).classes("text-sm text-gray-700")
                if job["temp_agency"]:
                    # The employer is a staffing firm and the work happens at a
                    # client's site — worth knowing before writing a letter, and
                    # the reason the letter never borrows this posting's address.
                    ui.label("⚠ Arbeitnehmerüberlassung — der Arbeitsort "
                             "gehört zu einem Kundenbetrieb.") \
                        .classes("text-sm text-amber-700")
                if job["liveness"] == liveness.LIVENESS_GONE:
                    checked = (job["liveness_checked_at"] or "")[:10]
                    ui.label(f"⚠ Anzeige offline — beim letzten Abruf am "
                             f"{checked} nicht mehr vorhanden.") \
                        .classes("text-sm text-red-700")
                already = applied.get(job["id"])
                if already:
                    ui.label(helpers.applied_line(already)).classes(
                        "text-sm text-amber-700")
                draft_text, draft_classes = _draft_line(
                    job["draft_status"], job["draft_updated_at"])
                if draft_text:
                    ui.label(draft_text).classes(draft_classes)
                channel_line = _apply_line(job)
                if channel_line:
                    ui.label(channel_line).classes("text-sm text-blue-700")
                if siblings:
                    render_siblings(job, siblings)
                description = job["description"] or "(no description available)"
                ui.markdown(posting_markdown(description[:4000])).classes("text-sm")
                with ui.row().classes("gap-2"):
                    open_url = _openable_url(job)
                    if open_url:
                        ui.button("Open posting", icon="open_in_new",
                                  on_click=lambda u=open_url:
                                      ui.navigate.to(u, new_tab=True)).props("outline")
                    if not job["apply_channel"]:
                        ui.button("Kanal ermitteln", icon="travel_explore",
                                  on_click=lambda j=job: resolve_channel(j)).props("outline")
                    if job["status"] in ("new", "portal"):
                        # the cockpit is where a FORM application actually
                        # happens; it opens the employer's page itself and
                        # keeps every field one click away beside it
                        ui.button("Formular ausfüllen", icon="assignment",
                                  on_click=lambda j=job:
                                      ui.navigate.to(f"/cockpit/{j['id']}")) \
                            .props("outline")
                    if (job["status"] in ("new", "portal")
                            and not already
                            and not _claim_is_running(job)):
                        # also at the form stage: the cockpit's own gaps tell him
                        # to draft, and opening a form moves the posting here.
                        # While one is genuinely being written the button can
                        # only refuse, so the line above says so instead — but
                        # once the claim goes stale this is the ONLY way back.
                        # `draft_button` is local to this render_job call, so the
                        # closure sees this row's own button and no other.
                        draft_button = ui.button("Draft application", icon="edit_note") \
                            .props("outline")
                        draft_button.on_click(
                            lambda j=job, b=draft_button: draft(j, button=b))
                    if job["status"] == "new":
                        if not job["contact_email"]:
                            ui.button("Kontakt-E-Mail suchen", icon="alternate_email",
                                      on_click=lambda j=job: find_email(j)).props("outline")
                        ui.button("Skip", icon="close",
                                  on_click=lambda j=job: skip(j)).props("outline color=grey")
                    if job["status"] == "portal":
                        ui.button("I applied — record it", icon="check",
                                  on_click=lambda j=job: confirm_applied(j)) \
                            .props("color=positive")

        def render_siblings(job: dict, siblings: list[dict]):
            """The postings this row stands in front of: same company, lower
            rank. Titles and scores only — one application per company means
            these are context for choosing, not rows to act on."""
            others = job.get("company_count", 1) - 1
            shown = ("" if others <= len(siblings)
                     else f" (die {len(siblings)} bestbewerteten)")
            stellen = "weitere Stelle" if others == 1 else "weitere Stellen"
            with ui.column().classes("gap-0 pl-3 border-l"):
                ui.label(
                    f"{others} {stellen} bei {job['company']}{shown} — "
                    "eine Bewerbung pro Firma, deshalb steht hier die "
                    "bestbewertete."
                ).classes("text-xs text-gray-500")
                for other in siblings:
                    with ui.row().classes("items-center gap-2"):
                        ui.label(f"{other['title']}{_score_line(other)}") \
                            .classes("text-xs")
                        other_url = _openable_url(other)
                        if other_url:
                            ui.button(
                                icon="open_in_new",
                                on_click=lambda u=other_url:
                                    ui.navigate.to(u, new_tab=True),
                            ).props("flat dense")

        def show_draft(draft_row: dict, job: dict):
            with overlay, ui.dialog() as dialog, \
                    ui.card().classes("w-[720px] max-w-full"):
                ui.label(f"Draft — {job['title']}").classes("font-bold")
                recipient = draft_row["recipient"] or \
                    "no application e-mail found (portal or manual contact)"
                ui.label(f"To: {recipient}").classes("text-sm text-gray-600")
                ui.input("Betreff", value=draft_row["betreff"]) \
                    .classes("w-full").props("readonly")
                ui.textarea("E-Mail", value=draft_row["email_body"]) \
                    .classes("w-full").props("readonly autogrow")
                ui.textarea("Anschreiben", value=draft_row["anschreiben_body"]) \
                    .classes("w-full").props("readonly autogrow")
                ui.label(
                    f"Model: {draft_row['llm_model']} · edit and send from "
                    f"the Review queue"
                ).classes("text-xs text-gray-500")
                pdf_label = ui.label(
                    f"Mappe: {draft_row['pdf_path']}" if draft_row["pdf_path"]
                    else ""
                ).classes("text-xs text-gray-600")
                with ui.row().classes("w-full justify-end gap-2"):
                    async def make_pdf():
                        say("Creating Bewerbungsmappe…")
                        result = await mappe.create_mappe(job["id"])
                        if not result["ok"]:
                            say(result["error"], type="warning",
                                      multi_line=True)
                            return
                        pdf_label.set_text(f"Mappe: {result['pdf_path']}")
                        say(
                            helpers.mappe_summary(result, with_anlagen=True),
                            type="positive", multi_line=True,
                        )
                        if result["warning"]:
                            say(result["warning"], type="warning",
                                      multi_line=True)

                    def open_pdf():
                        path = (pdf_label.text or "").removeprefix("Mappe: ")
                        if not path:
                            say("create the Mappe first", type="warning")
                        elif not pathlib.Path(path).exists():
                            say("the Mappe file is gone — create it "
                                      "again", type="warning")
                        else:
                            open_in_system(path)

                    ui.button("Create PDF", icon="picture_as_pdf",
                              on_click=make_pdf).props("outline")
                    ui.button("Open PDF", icon="open_in_new",
                              on_click=open_pdf).props("outline")
                    ui.button("Re-draft", icon="refresh",
                              on_click=lambda: redraft(dialog, job)) \
                        .props("outline")
                    ui.button("Review queue", icon="outbox",
                              on_click=lambda: ui.navigate.to("/queue")) \
                        .props("outline")
                    ui.button("Close", on_click=dialog.close).props("flat")
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
            say("Drafting application…")
            if button is not None:
                # A minute is long enough that a faded toast reads as "nothing
                # happened" — the button that was pressed says what it is doing.
                button.set_text("wird geschrieben…")
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
            # The row carries the outcome from here on, whichever way it went —
            # and everything the user sees afterwards is built in `overlay`,
            # because this refresh has just deleted the button we came from.
            await refresh()
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
            await refresh()
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
                    await refresh()

                with ui.row().classes("w-full justify-end gap-2"):
                    if not res["generic"]:  # never adopt a generic info@ inbox
                        ui.button("Übernehmen", icon="check",
                                  on_click=adopt).props("color=positive")
                    ui.button("Schließen", on_click=dialog.close).props("flat")
            dialog.open()

        async def skip(job: dict):
            await run.io_bound(_set_status, job["id"], "skipped")
            await refresh()

        async def confirm_applied(job: dict):
            bewerbung_id = await run.io_bound(_confirm_applied, job["id"], "Online-Portal")
            await refresh()
            if bewerbung_id is None:
                say("Blocked: you already applied at this company", type="warning")
            else:
                say("Application recorded ✓", type="positive")

        with ui.row().classes("items-center gap-4"):
            ui.select(
                {view.key: view.label for view in VIEWS},
                value=DEFAULT_VIEW,
                label="Ansicht",
                on_change=lambda e: set_view(e.value),
            ).mark("view-select").props("dense outlined").classes("min-w-48") \
                .tooltip("Eine Ansicht statt sechs Filtern und vier Schaltern. "
                         "Nichts wird je gelöscht — was hier nicht steht, steht "
                         "in einer der anderen Ansichten.")
            hidden_label = ui.label().classes("text-xs text-gray-500")
        # Postings arrive hourly, scores land every ten minutes and the liveness
        # pass runs 90 s after every start — all of it invisible until this. It
        # rebuilds only when the data really changed, and never while he has a
        # posting open or a dialog on screen.
        with header:
            live_view = live.watch(
                _signature, refresh,
                busy=lambda: reading["rows"] > 0 or live.dialog_open(),
            )

        async def set_view(value: str):
            """Move to another named view.

            One assignment and one refresh: nothing here writes another control,
            so no handler can be echoed back into this one — which is what made
            the two pile switches rebuild the page forever."""
            current["view"] = value or DEFAULT_VIEW
            page["value"] = 0  # a different list: page 3 of it means nothing
            await refresh()

        await refresh()
