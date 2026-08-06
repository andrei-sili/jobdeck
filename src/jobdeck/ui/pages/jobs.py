"""Job inbox: discovered postings with per-job actions."""

import pathlib

from nicegui import run, ui

from jobdeck import apply_channel, db
from jobdeck.services import apply_resolve, contact_lookup, drafting, liveness, mappe
from jobdeck.ui import helpers
from jobdeck.ui.helpers import (
    open_in_system,
    openable_url,
    posting_markdown,
)
from jobdeck.ui.layout import frame

FILTERS = ["new", "portal", "duplicate", "skipped", "applied", "all"]

# One page of postings. It used to be a hard limit of 100 with no way past it,
# which left 187 of his 287 open postings unreachable in the UI entirely — and
# no number on screen said so. Quasar renders an expansion's content EAGERLY, so
# a page is a real cost: pages plus a printed total beat one long list.
PAGE_SIZE = 50

# Two piles are hidden from the working inbox, on the same terms: a score-0
# mismatch violates a stated hard requirement, and a 'gone' posting's ad is no
# longer online. Both are FACTS about the posting, so they hide it — and neither
# ever deletes it. Opening a pile is a separate VIEW of just that pile, not a
# filter that stacks with the other: mixing either back into the list would
# leave it unreachable once better-scored rows fill the page.
PILE_NONE, PILE_MISMATCHES, PILE_DEAD = "", "mismatches", "dead"
_PILE_FILTERS = {
    PILE_NONE: {"mismatches": "exclude", "gone": "exclude"},
    PILE_MISMATCHES: {"mismatches": "only", "gone": "include"},
    PILE_DEAD: {"mismatches": "include", "gone": "only"},
}


_EMPTY_VIEW = {
    PILE_NONE: "Nothing here. Run a search profile to discover jobs.",
    PILE_MISMATCHES: "No mismatches — nothing is hidden.",
    PILE_DEAD: "No dead postings — every ad checked so far is still online.",
}


def _view_filters(pile: str, status: str) -> dict:
    """The pile filters this view really uses.

    Hiding is for the WORKING list. Once he has acted on a posting — opened its
    portal, applied, skipped it — its row carries the action that finishes the
    job ("I applied — record it"), so hiding it would hide that button. Only the
    `new` view hides; every other filter shows what it contains."""
    if pile == PILE_NONE and status != "new":
        return {"mismatches": "include", "gone": "include"}
    return _PILE_FILTERS[pile]


def _hidden_line(filters: dict, mismatches: int, dead: int) -> str:
    """What this view is not showing, derived from the filters it actually used
    so the label cannot contradict the list. Two independent statements, never
    a total: a posting can be both a mismatch and offline."""
    parts = []
    if filters["mismatches"] == "exclude" and mismatches:
        parts.append(f"{mismatches} mismatches hidden")
    elif filters["mismatches"] == "only":
        parts.append(f"{mismatches} mismatches — hard requirement violated")
    if filters["gone"] == "exclude" and dead:
        parts.append(f"{dead} dead hidden")
    elif filters["gone"] == "only":
        parts.append(f"{dead} postings whose ad is gone")
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
    aged = f" · {age} Tage alt" if age is not None else " · Datum unbekannt"
    if effective != score:
        return f" · match {score} → {effective}{aged}"
    return f" · match {score}{aged}"


def _load_jobs(status: str, pile: str, page: int, collapse: bool = True) -> dict:
    """One page of the current view, with everything needed to describe it.

    Collapsed, a row stands for a COMPANY — its best-ranked posting, with the
    others listed beneath it. The count and the page therefore both count
    companies, so the printed range stays true to what is on screen.

    The page number is clamped HERE, against the total this very query saw: a
    filter change or a background poll can shrink the result set under the
    user's feet, and asking for page 5 of a two-page list must show the last
    page rather than an empty one."""
    with db.db() as con:
        status_arg = None if status == "all" else status
        filters = _view_filters(pile, status)
        count = db.count_job_groups if collapse else db.count_jobs
        total = count(con, status_arg, **filters)
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        listing = db.list_job_groups if collapse else db.list_jobs
        rows = [dict(r) for r in listing(
            con, status_arg, limit=PAGE_SIZE, offset=page * PAGE_SIZE, **filters)]
        siblings: dict[str, list[dict]] = {}
        if collapse:
            keys = [r["company_key"] for r in rows if r["company_count"] > 1]
            for row in db.list_company_siblings(con, keys, status_arg, **filters):
                siblings.setdefault(row["company_key"], []).append(dict(row))
        return {
            "rows": rows,
            "siblings": siblings,
            "filters": filters,
            "collapse": collapse,
            "mismatches": db.count_mismatches(con, status_arg),
            "dead": db.count_gone_jobs(con, status_arg),
            "total": total,
            "page": page,
            "pages": pages,
        }


def _range_line(page: int, total: int, shown: int, collapse: bool) -> str:
    """'51–100 von 266 Firmen' — where in the pipeline this page sits.

    The unit is named because it changes with the grouping toggle: a grouped
    page counts companies while the hidden-pile counts beside it are postings,
    and an unlabelled pair of numbers invites comparing them."""
    if not total:
        return ""
    first = page * PAGE_SIZE + 1
    unit = "Firmen" if collapse else "Stellen"
    return f"{first}–{first + shown - 1} von {total} {unit}"


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


def _openable_url(job: dict) -> str:
    """The URL a posting's buttons may hand to the browser, '' when none is
    safe. The resolved apply link wins over the raw feed URL."""
    return openable_url(job["apply_url"] or job["url"] or "")


@ui.page("/jobs")
async def jobs_page():
    with frame("Job inbox"):
        status_filter = {"value": "new"}
        pile = {"value": PILE_NONE}
        collapse = {"value": True}
        page = {"value": 0}
        refresh_gen = {"n": 0}  # rapid filter/switch flips: last request wins
        container = ui.column().classes("w-full gap-2")
        pager = ui.row().classes("items-center gap-2")

        async def refresh():
            refresh_gen["n"] += 1
            gen = refresh_gen["n"]
            view = await run.io_bound(
                _load_jobs, status_filter["value"], pile["value"], page["value"],
                collapse["value"],
            )
            if gen != refresh_gen["n"]:
                return  # superseded — a newer refresh already owns the view
            page["value"] = view["page"]  # the loader clamped it to what exists
            container.clear()
            hidden_label.set_text(
                _hidden_line(view["filters"], view["mismatches"], view["dead"]))
            with container:
                if not view["rows"]:
                    ui.label(_EMPTY_VIEW[pile["value"]]).classes("text-gray-500")
                for job in view["rows"]:
                    render_job(job, view["siblings"].get(job.get("company_key"), []))
            render_pager(view)

        def render_pager(view: dict):
            pager.clear()
            with pager:
                ui.label(_range_line(view["page"], view["total"],
                                     len(view["rows"]), view["collapse"])) \
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

        def render_job(job: dict, siblings: list[dict] = ()):
            remote = " · remote" if job["remote"] else ""
            head = (f"{job['title']}  —  {job['company']}"
                    f" ({job['location'] or 'n/a'}{remote}{_score_line(job)})")
            others = job.get("company_count", 1) - 1
            if others > 0:
                head += f"  +{others}"
            with ui.expansion(head).classes("w-full border rounded"):
                ui.label(f"Source: {job['source']} · found {job['fetched_at'][:16]} · "
                         f"status: {job['status']}").classes("text-xs text-gray-500")
                if job["match_reason"]:
                    mismatch = job["match_score"] == 0
                    ui.label(f"Match: {job['match_reason']}").classes(
                        "text-sm text-red-700" if mismatch else "text-sm text-gray-600"
                    )
                if job["contact_email"]:
                    ui.label(f"Contact: {job['contact_email']}").classes("text-sm")
                if job["liveness"] == liveness.LIVENESS_GONE:
                    checked = (job["liveness_checked_at"] or "")[:10]
                    ui.label(f"⚠ Anzeige offline — beim letzten Abruf am "
                             f"{checked} nicht mehr vorhanden.") \
                        .classes("text-sm text-red-700")
                if job["duplicate_of"]:
                    ui.label("⚠ You already applied at this company — see Applications.") \
                        .classes("text-sm text-amber-700")
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
                    if job["status"] == "new":
                        ui.button("Draft application", icon="edit_note",
                                  on_click=lambda j=job: draft(j)).props("outline")
                        ui.button("Apply via portal", icon="language",
                                  on_click=lambda j=job: mark_portal(j)).props("outline")
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
            with ui.dialog() as dialog, ui.card().classes("w-[720px] max-w-full"):
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
                        ui.notify("Creating Bewerbungsmappe…")
                        result = await mappe.create_mappe(job["id"])
                        if not result["ok"]:
                            ui.notify(result["error"], type="warning",
                                      multi_line=True)
                            return
                        pdf_label.set_text(f"Mappe: {result['pdf_path']}")
                        ui.notify(
                            helpers.mappe_summary(result, with_anlagen=True),
                            type="positive", multi_line=True,
                        )
                        if result["warning"]:
                            ui.notify(result["warning"], type="warning",
                                      multi_line=True)

                    def open_pdf():
                        path = (pdf_label.text or "").removeprefix("Mappe: ")
                        if not path:
                            ui.notify("create the Mappe first", type="warning")
                        elif not pathlib.Path(path).exists():
                            ui.notify("the Mappe file is gone — create it "
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

        async def draft(job: dict, force: bool = False):
            # a finished draft costs nothing to show again — regenerate only
            # on explicit request
            if not force:
                existing = await run.io_bound(_load_draft, job["id"])
                if existing is not None and existing["status"] == "ready":
                    show_draft(existing, job)
                    return
            ui.notify("Drafting application…")
            result = await drafting.draft_for_job(job["id"])
            if not result["ok"]:
                ui.notify(result["error"], type="warning", multi_line=True)
                return
            show_draft(result["draft"], job)

        async def resolve_channel(job: dict):
            ui.notify("Bewerbungskanal wird ermittelt…")
            res = await apply_resolve.resolve_and_store(job["id"])
            label = _apply_line({**job, "apply_channel": res["channel"],
                                 "ats_vendor": res["vendor"]})
            ui.notify(label or "Kanal nicht ermittelbar",
                      type="positive" if label else "warning")
            await refresh()

        async def find_email(job: dict):
            ui.notify("Kontakt-E-Mail wird gesucht…")
            res = await contact_lookup.lookup_and_propose(job["id"])
            if not res["email"]:
                ui.notify("Keine verifizierte Bewerbungs-E-Mail gefunden",
                          type="warning")
                return
            with ui.dialog() as dialog, ui.card().classes("w-[440px] max-w-full"):
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
                    ui.notify(f"Übernommen: {res['email']}", type="positive")
                    dialog.close()
                    await refresh()

                with ui.row().classes("w-full justify-end gap-2"):
                    if not res["generic"]:  # never adopt a generic info@ inbox
                        ui.button("Übernehmen", icon="check",
                                  on_click=adopt).props("color=positive")
                    ui.button("Schließen", on_click=dialog.close).props("flat")
            dialog.open()

        async def mark_portal(job: dict):
            await run.io_bound(_set_status, job["id"], "portal")
            open_url = _openable_url(job)
            if open_url:
                ui.navigate.to(open_url, new_tab=True)
            else:
                ui.notify("No safe URL stored for this posting — open it manually.",
                          type="warning")
            await refresh()

        async def skip(job: dict):
            await run.io_bound(_set_status, job["id"], "skipped")
            await refresh()

        async def confirm_applied(job: dict):
            bewerbung_id = await run.io_bound(_confirm_applied, job["id"], "Online-Portal")
            if bewerbung_id is None:
                ui.notify("Blocked: you already applied at this company", type="warning")
            else:
                ui.notify("Application recorded ✓", type="positive")
            await refresh()

        pile_switches: dict[str, ui.switch] = {}
        with ui.row().classes("items-center gap-4"):
            ui.toggle(
                FILTERS,
                value="new",
                on_change=lambda e: set_filter(e.value),
            )
            pile_switches[PILE_MISMATCHES] = ui.switch(
                "Show mismatches",
                value=False,
                on_change=lambda e: set_pile(PILE_MISMATCHES, e.value),
            ).tooltip("Show the hidden pile: postings scored 0 for violating "
                      "a hard requirement — hidden, never deleted")
            pile_switches[PILE_DEAD] = ui.switch(
                "Show dead postings",
                value=False,
                on_change=lambda e: set_pile(PILE_DEAD, e.value),
            ).tooltip("Show the hidden pile: postings whose ad the board says "
                      "is gone — hidden, never deleted")
            ui.switch(
                "Group by company",
                value=True,
                on_change=lambda e: set_collapse(e.value),
            ).tooltip("One row per company, showing its best-scored posting — "
                      "only one application per company is possible anyway")
            hidden_label = ui.label().classes("text-xs text-gray-500")

        async def set_collapse(value: bool):
            collapse["value"] = value
            page["value"] = 0  # companies and postings are not the same count
            await refresh()

        async def set_filter(value: str):
            status_filter["value"] = value
            page["value"] = 0  # a different list: page 3 of it means nothing
            await refresh()

        async def set_pile(name: str, on: bool):
            """Open or close one hidden pile. The two are separate views, so
            opening one closes the other — and the switch this turns off calls
            straight back in, which the second branch absorbs."""
            if on:
                pile["value"] = name
            elif pile["value"] == name:
                pile["value"] = PILE_NONE
            else:
                return  # closed by us to open the other pile: nothing to do
            page["value"] = 0
            for other, switch in pile_switches.items():
                wanted = pile["value"] == other
                if switch.value != wanted:
                    switch.value = wanted
            await refresh()

        await refresh()
