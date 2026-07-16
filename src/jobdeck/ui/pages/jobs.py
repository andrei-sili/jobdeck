"""Job inbox: discovered postings with per-job actions."""

from nicegui import run, ui

from jobdeck import db
from jobdeck.ui.layout import frame

FILTERS = ["new", "portal", "duplicate", "skipped", "applied", "all"]
PAGE_LIMIT = 100


def _load_jobs(status: str, include_mismatches: bool):
    """Inbox rows plus how many score-0 mismatches the filter is hiding."""
    with db.db() as con:
        status_arg = None if status == "all" else status
        rows = db.list_jobs(
            con, status_arg, limit=PAGE_LIMIT,
            include_mismatches=include_mismatches,
        )
        hidden = 0 if include_mismatches else db.count_mismatches(con, status_arg)
        return [dict(r) for r in rows], hidden


def _set_status(job_id: int, status: str):
    with db.db() as con:
        db.set_job_status(con, job_id, status)


def _confirm_applied(job_id: int, kanal: str):
    with db.db() as con:
        return db.apply_job(con, job_id, kanal=kanal)


@ui.page("/jobs")
async def jobs_page():
    with frame("Job inbox"):
        status_filter = {"value": "new"}
        show_mismatches = {"value": False}
        container = ui.column().classes("w-full gap-2")

        async def refresh():
            container.clear()
            jobs, hidden = await run.io_bound(
                _load_jobs, status_filter["value"], show_mismatches["value"]
            )
            hidden_label.set_text(f"{hidden} mismatches hidden" if hidden else "")
            with container:
                if not jobs:
                    ui.label("Nothing here. Run a search profile to discover jobs.") \
                        .classes("text-gray-500")
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
                        ui.button("Draft application", icon="edit_note") \
                            .props("outline") \
                            .tooltip("AI drafting arrives in Phase 2") \
                            .disable()
                        ui.button("Apply via portal", icon="language",
                                  on_click=lambda j=job: mark_portal(j)).props("outline")
                        ui.button("Skip", icon="close",
                                  on_click=lambda j=job: skip(j)).props("outline color=grey")
                    if job["status"] == "portal":
                        ui.button("I applied — record it", icon="check",
                                  on_click=lambda j=job: confirm_applied(j)) \
                            .props("color=positive")

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
            ).tooltip("Postings that violate a hard requirement (score 0) — "
                      "hidden, never deleted")
            hidden_label = ui.label().classes("text-xs text-gray-500")

        async def set_filter(value: str):
            status_filter["value"] = value
            await refresh()

        async def set_mismatches(value: bool):
            show_mismatches["value"] = value
            await refresh()

        await refresh()
