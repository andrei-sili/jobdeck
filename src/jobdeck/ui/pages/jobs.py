"""Job inbox: discovered postings with per-job actions."""

from nicegui import run, ui

from jobdeck import db
from jobdeck.services import drafting
from jobdeck.ui.layout import frame

FILTERS = ["new", "portal", "duplicate", "skipped", "applied", "all"]
PAGE_LIMIT = 100


def _load_jobs(status: str, show_mismatches: bool):
    """Inbox rows plus how many score-0 mismatches the filter is hiding.

    The mismatch view lists ONLY the hidden pile: mixing mismatches into the
    normal list would leave them unreachable once better-scored rows fill
    PAGE_LIMIT (score 0 sorts last)."""
    with db.db() as con:
        status_arg = None if status == "all" else status
        rows = db.list_jobs(
            con, status_arg, limit=PAGE_LIMIT,
            mismatches="only" if show_mismatches else "exclude",
        )
        hidden = 0 if show_mismatches else db.count_mismatches(con, status_arg)
        return [dict(r) for r in rows], hidden


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


@ui.page("/jobs")
async def jobs_page():
    with frame("Job inbox"):
        status_filter = {"value": "new"}
        show_mismatches = {"value": False}
        refresh_gen = {"n": 0}  # rapid filter/switch flips: last request wins
        container = ui.column().classes("w-full gap-2")

        async def refresh():
            refresh_gen["n"] += 1
            gen = refresh_gen["n"]
            jobs, hidden = await run.io_bound(
                _load_jobs, status_filter["value"], show_mismatches["value"]
            )
            if gen != refresh_gen["n"]:
                return  # superseded — a newer refresh already owns the view
            container.clear()
            hidden_label.set_text(f"{hidden} mismatches hidden" if hidden else "")
            with container:
                if not jobs:
                    empty = ("No mismatches — nothing is hidden."
                             if show_mismatches["value"]
                             else "Nothing here. Run a search profile to "
                                  "discover jobs.")
                    ui.label(empty).classes("text-gray-500")
                for job in jobs:
                    render_job(job)

        def render_job(job: dict):
            score = f" · match {job['match_score']}" if job["match_score"] is not None else ""
            remote = " · remote" if job["remote"] else ""
            head = (f"{job['title']}  —  {job['company']}"
                    f" ({job['location'] or 'n/a'}{remote}{score})")
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
                if job["duplicate_of"]:
                    ui.label("⚠ You already applied at this company — see Applications.") \
                        .classes("text-sm text-amber-700")
                description = job["description"] or "(no description available)"
                ui.markdown(description[:4000]).classes("text-sm")
                with ui.row().classes("gap-2"):
                    ui.button("Open posting", icon="open_in_new",
                              on_click=lambda url=job["url"]: ui.navigate.to(url, new_tab=True)) \
                        .props("outline")
                    if job["status"] == "new":
                        ui.button("Draft application", icon="edit_note",
                                  on_click=lambda j=job: draft(j)).props("outline")
                        ui.button("Apply via portal", icon="language",
                                  on_click=lambda j=job: mark_portal(j)).props("outline")
                        ui.button("Skip", icon="close",
                                  on_click=lambda j=job: skip(j)).props("outline color=grey")
                    if job["status"] == "portal":
                        ui.button("I applied — record it", icon="check",
                                  on_click=lambda j=job: confirm_applied(j)) \
                            .props("color=positive")

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
                    f"Model: {draft_row['llm_model']} · editing and sending "
                    f"arrive with the review queue"
                ).classes("text-xs text-gray-500")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Re-draft", icon="refresh",
                              on_click=lambda: redraft(dialog, job)) \
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

        async def mark_portal(job: dict):
            await run.io_bound(_set_status, job["id"], "portal")
            ui.navigate.to(job["url"], new_tab=True)
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

        with ui.row().classes("items-center gap-4"):
            ui.toggle(
                FILTERS,
                value="new",
                on_change=lambda e: set_filter(e.value),
            )
            ui.switch(
                "Show mismatches",
                value=False,
                on_change=lambda e: set_mismatches(e.value),
            ).tooltip("Show the hidden pile: postings scored 0 for violating "
                      "a hard requirement — hidden, never deleted")
            hidden_label = ui.label().classes("text-xs text-gray-500")

        async def set_filter(value: str):
            status_filter["value"] = value
            await refresh()

        async def set_mismatches(value: bool):
            show_mismatches["value"] = value
            await refresh()

        await refresh()
