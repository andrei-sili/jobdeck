"""The apply cockpit: one narrow screen that parks beside the employer's form.

Portals and ATS forms are never automated, so the five minutes of typing is the
work that remains — and 237 of his postings route to a form. Every field the
form asks for is one click to the clipboard here, in the order a German form
tends to ask, with the Mappe reachable so he can attach it.

Deliberately NOT a wizard and NOT a filler: it never touches the employer's page.
It only makes his own answers instant to hand over, and it records the
application when he says he sent it.
"""

import pathlib

from nicegui import run, ui

from jobdeck import apply_channel, apply_form, db
from jobdeck.constants import LIVENESS_GONE
from jobdeck.ui.helpers import open_in_system, openable_url
from jobdeck.ui.layout import frame


def _load(job_id: int) -> dict | None:
    """The posting, its draft and the applicant settings in one read."""
    with db.db() as con:
        job = db.get_job(con, job_id)
        if job is None:
            return None
        draft = db.get_draft_by_job(con, job_id)
        settings = {key: db.get_setting(con, key, "")
                    for key in apply_form.APPLICANT_SETTINGS}
        return {
            "job": dict(job),
            "draft": dict(draft) if draft is not None else None,
            "settings": settings,
        }


def _mark_portal(job_id: int) -> None:
    """Move a posting to 'portal' when he opens its form — and only from 'new':
    a posting already applied to or skipped must not be dragged back."""
    with db.db() as con:
        job = db.get_job(con, job_id)   # it can be gone by the time he clicks
        if job is not None and job["status"] == "new":
            db.set_job_status(con, job_id, JOB_NEW_STATUS)


JOB_NEW_STATUS = "portal"       # where a form application lives while unconfirmed
RECORDABLE_STATUS = ("new", "portal")   # what "Beworben — eintragen" may finish


def _record(job_id: int, kanal: str):
    with db.db() as con:
        return db.apply_job(con, job_id, kanal=kanal)


def _channel_line(job: dict) -> str:
    channel, vendor = job["apply_channel"] or "", job["ats_vendor"] or ""
    if channel == apply_channel.CHANNEL_DIRECT_EMAIL:
        return "Direkt per E-Mail — die Review-Queue schickt sie."
    if channel in (apply_channel.CHANNEL_ATS, apply_channel.CHANNEL_BOARD):
        return f"Formular bei {vendor}" if vendor else "Formular in einem Portal"
    if channel == apply_channel.CHANNEL_COMPANY_SITE:
        return "Formular auf der Firmen-Website"
    return "Kanal noch nicht ermittelt"


@ui.page("/cockpit/{job_id}")
async def cockpit_page(job_id: int):
    with frame("Bewerbung ausfüllen"):
        view = await run.io_bound(_load, job_id)
        if view is None:
            ui.label(f"Posting {job_id} does not exist.").classes("text-gray-500")
            return
        job, draft = view["job"], view["draft"]
        rows = apply_form.fields(job, draft, view["settings"])
        gaps = apply_form.missing(rows)

        ui.label(f"{job['company']}").classes("text-lg font-bold")
        ui.label(job["title"]).classes("text-sm")
        ui.label(_channel_line(job)).classes("text-sm text-blue-700")

        async def open_form(url: str):
            # opening the form is the moment he starts applying: record it, so
            # the posting leaves the working inbox and its liveness keeps being
            # checked while he is at the form
            await run.io_bound(_mark_portal, job_id)
            ui.navigate.to(url, new_tab=True)

        def copy(field: apply_form.Field):
            ui.clipboard.write(field.value)
            ui.notify(f"{field.label} kopiert", type="positive")

        async def record_applied():
            bewerbung_id = await run.io_bound(_record, job_id, "Online-Portal")
            if bewerbung_id is None:
                ui.notify("Blocked: you already applied at this company",
                          type="warning")
                return
            ui.notify("Application recorded ✓", type="positive")
            ui.navigate.to("/jobs")

        if job["liveness"] == LIVENESS_GONE:
            checked = (job["liveness_checked_at"] or "")[:10]
            ui.label(f"⚠ Die Anzeige war am {checked} nicht mehr online — vor "
                     "dem Ausfüllen prüfen.").classes("text-sm text-red-700")

        with ui.row().classes("items-center gap-2"):
            form_url = openable_url(job["apply_url"] or job["url"] or "")
            if form_url:
                ui.button("Formular öffnen", icon="open_in_new",
                          on_click=lambda: open_form(form_url)).props("color=primary")
            else:
                ui.label("No safe URL stored — open the posting manually.") \
                    .classes("text-sm text-amber-700")

        if gaps:
            ui.label("Fehlt noch: " + ", ".join(f.label for f in gaps)) \
                .classes("text-sm text-amber-700")

        with ui.column().classes("w-full gap-1 mt-2"):
            for field in rows:
                with ui.row().classes("w-full items-start gap-2 border-b py-1"):
                    ui.label(field.label).classes("text-xs text-gray-500 w-40 shrink-0")
                    if field.ready:
                        text = field.value
                        shown = (text if not field.multiline
                                 else text[:160] + ("…" if len(text) > 160 else ""))
                        ui.label(shown).classes("text-sm grow break-all")
                        ui.button(icon="content_copy",
                                  on_click=lambda f=field: copy(f)) \
                            .props("flat dense").tooltip("In die Zwischenablage")
                    else:
                        ui.label(field.hint).classes("text-sm text-amber-700 grow")

        pdf_field = next((f for f in rows
                          if f.label.startswith("Bewerbungsmappe")), None)
        with ui.row().classes("items-center gap-2 mt-2"):
            if pdf_field is not None and pdf_field.ready:
                ui.button("Mappe-Ordner öffnen", icon="folder_open",
                          on_click=lambda: open_in_system(
                              str(pathlib.Path(pdf_field.value).parent))) \
                    .props("outline") \
                    .tooltip("Der Ordner, damit die Datei greifbar ist")
            if job["status"] in RECORDABLE_STATUS:
                ui.button("Beworben — eintragen", icon="check",
                          on_click=record_applied).props("color=positive")
            else:
                # already applied, skipped or a duplicate: a second recording
                # would make this posting a 'duplicate' of its own application
                ui.label(f"Status: {job['status']} — nichts mehr einzutragen.") \
                    .classes("text-sm text-gray-500")
            ui.button("Zurück zum Inbox", icon="arrow_back",
                      on_click=lambda: ui.navigate.to("/jobs")).props("flat")
