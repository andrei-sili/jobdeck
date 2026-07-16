"""Review queue: edit, approve and send drafted applications.

Every send is human-approved here (auto-send only transmits drafts the
user explicitly approved). The page is loud about the sending mode: while
real sending is OFF, the banner and the pre-send confirmation both show
the test recipient every message will actually go to.
"""

import pathlib

from nicegui import run, ui

from jobdeck import db
from jobdeck.services import mappe, send
from jobdeck.ui.helpers import open_in_system
from jobdeck.ui.layout import frame

FILTERS = ["open", "sent", "discarded"]
FILTER_STATUSES = {
    "open": ["ready", "approved", "sending"],
    "sent": ["sent"],
    "discarded": ["discarded"],
}
EMPTY_TEXT = {
    "open": "No drafts waiting. Draft an application from the Job inbox first.",
    "sent": "Nothing sent yet.",
    "discarded": "No discarded drafts.",
}


def _load_drafts(filter_value: str):
    with db.db() as con:
        rows = db.list_drafts_with_jobs(con, FILTER_STATUSES[filter_value])
        return [dict(r) for r in rows]


def _send_status():
    with db.db() as con:
        return {
            "real": db.get_setting(con, "real_send_enabled", "0") == "1",
            "test_recipient": db.get_setting(con, "test_recipient", "").strip(),
            "cap": db.get_setting(con, "daily_send_cap", "15"),
            "sent_today": db.count_outbound_today(con),
        }


def _save_draft(job_id: int, values: dict, clear_pdf: bool):
    with db.db() as con:
        if clear_pdf:
            values = {**values, "pdf_path": ""}
        db.upsert_draft(con, job_id, values)
        return dict(db.get_draft_by_job(con, job_id))


@ui.page("/queue")
async def queue_page():
    with frame("Review queue"):
        filter_state = {"value": "open"}
        refresh_gen = {"n": 0}  # rapid filter flips: last request wins

        banner = ui.row().classes("w-full items-center gap-2")
        with ui.row().classes("items-center gap-4"):
            ui.toggle(FILTERS, value="open",
                      on_change=lambda e: set_filter(e.value))
        container = ui.column().classes("w-full gap-2")

        async def refresh():
            refresh_gen["n"] += 1
            gen = refresh_gen["n"]
            drafts = await run.io_bound(_load_drafts, filter_state["value"])
            status = await run.io_bound(_send_status)
            if gen != refresh_gen["n"]:
                return  # superseded — a newer refresh already owns the view
            banner.clear()
            with banner:
                if status["real"]:
                    ui.label("REAL sending is ON — e-mails go to the "
                             "companies.").classes(
                        "text-sm font-bold text-red-700")
                else:
                    target = status["test_recipient"] or \
                        "nobody — set a test recipient in Settings"
                    ui.label(f"TEST MODE — every send goes to: {target}") \
                        .classes("text-sm font-bold text-amber-700")
                ui.label(f"{status['sent_today']}/{status['cap']} sent today") \
                    .classes("text-xs text-gray-500")
            container.clear()
            with container:
                if not drafts:
                    ui.label(EMPTY_TEXT[filter_state["value"]]) \
                        .classes("text-gray-500")
                for row in drafts:
                    render_draft(row)

        async def set_filter(value: str):
            filter_state["value"] = value
            await refresh()

        def render_draft(row: dict):
            score = (f" · match {row['job_score']}"
                     if row["job_score"] is not None else "")
            head = (f"{row['job_company']}  —  {row['job_title']}"
                    f"  ({row['status']}{score})")
            with ui.expansion(head).classes("w-full border rounded"):
                ui.label(f"To: {row['recipient'] or '(no recipient yet)'} · "
                         f"updated {row['updated_at'][:16]}") \
                    .classes("text-xs text-gray-500")
                ui.label(row["betreff"]).classes("text-sm")
                if row["pdf_path"]:
                    ui.label(f"Mappe: {row['pdf_path']}") \
                        .classes("text-xs text-gray-600")
                else:
                    ui.label("No Mappe PDF yet — required before sending.") \
                        .classes("text-xs text-amber-700")
                if row["error"]:
                    ui.label(row["error"]).classes("text-sm text-red-700")
                with ui.row().classes("gap-2"):
                    if row["status"] in ("ready", "approved"):
                        ui.button("Review & send", icon="edit_note",
                                  on_click=lambda r=row: show_editor(r)) \
                            .props("outline")
                        if row["status"] == "ready":
                            ui.button("Approve for auto-send", icon="schedule_send",
                                      on_click=lambda r=row: approve(r)) \
                                .props("outline")
                        else:
                            ui.button("Return to ready", icon="undo",
                                      on_click=lambda r=row: unapprove(r)) \
                                .props("outline")
                        ui.button("Discard", icon="delete",
                                  on_click=lambda r=row: discard(r)) \
                            .props("outline color=grey")
                    if row["status"] == "sending":
                        ui.label("A send is in progress — or it died mid-way. "
                                 "Check the Gmail 'Sent' folder before "
                                 "resolving. A stuck TEST send is always "
                                 "'not sent'.").classes("text-sm text-amber-700")
                        ui.button("Not sent — return to ready", icon="undo",
                                  on_click=lambda r=row: resolve(r, False)) \
                            .props("outline")
                        ui.button("It was sent — record it", icon="check",
                                  on_click=lambda r=row: resolve(r, True)) \
                            .props("outline color=positive")
                    if row["status"] == "sent":
                        sent_info = f"sent {row['updated_at'][:16]}"
                        if row["gmail_message_id"]:
                            sent_info += f" · gmail id {row['gmail_message_id']}"
                        ui.label(sent_info).classes("text-xs text-gray-500")
                        if row["pdf_path"]:
                            ui.button("Open PDF", icon="open_in_new",
                                      on_click=lambda r=row:
                                      open_in_system(r["pdf_path"])) \
                                .props("outline")
                    if row["status"] == "discarded":
                        ui.button("Restore", icon="restore",
                                  on_click=lambda r=row: restore(r)) \
                            .props("outline")
                    ui.button("Open posting", icon="open_in_new",
                              on_click=lambda r=row:
                              ui.navigate.to(r["job_url"], new_tab=True)) \
                        .props("flat")

        async def _simple_action(action, job_id: int, success: str):
            result = await run.io_bound(action, job_id)
            if not result["ok"]:
                ui.notify(result["error"], type="warning", multi_line=True)
            else:
                ui.notify(success, type="positive")
            await refresh()

        async def approve(row: dict):
            await _simple_action(send.approve, row["job_id"],
                                 "Approved — auto-send will pick it up")

        async def unapprove(row: dict):
            await _simple_action(send.unapprove, row["job_id"],
                                 "Returned to ready")

        async def discard(row: dict):
            await _simple_action(send.discard, row["job_id"], "Discarded")

        async def restore(row: dict):
            await _simple_action(send.restore, row["job_id"], "Restored")

        async def resolve(row: dict, assume_sent: bool):
            if assume_sent:
                with ui.dialog() as confirm, ui.card():
                    ui.label("Record as sent?").classes("font-bold")
                    ui.label("Only if the Gmail 'Sent' folder shows this "
                             "message went out. It will be recorded without "
                             "Gmail ids.").classes("text-sm")
                    with ui.row().classes("justify-end gap-2 w-full"):
                        ui.button("Cancel",
                                  on_click=lambda: confirm.submit(False)) \
                            .props("flat")
                        ui.button("Record as sent",
                                  on_click=lambda: confirm.submit(True)) \
                            .props("color=positive")
                confirm.open()
                if not await confirm:
                    return
            await _simple_action(
                lambda job_id: send.resolve_sending(job_id, assume_sent),
                row["job_id"],
                "Recorded as sent" if assume_sent else "Returned to ready",
            )

        def show_editor(row: dict):
            job_id = row["job_id"]
            current = dict(row)
            with ui.dialog() as dialog, ui.card().classes("w-[760px] max-w-full"):
                ui.label(f"{row['job_company']} — {row['job_title']}") \
                    .classes("font-bold")
                hint = (f"posting contact: {row['job_contact_email']}"
                        if row["job_contact_email"] else
                        "no contact e-mail found in the posting")
                recipient = ui.input(f"Recipient ({hint})",
                                     value=row["recipient"]).classes("w-full")
                betreff = ui.input("Betreff", value=row["betreff"]) \
                    .classes("w-full")
                email_body = ui.textarea("E-Mail", value=row["email_body"]) \
                    .classes("w-full").props("autogrow")
                anschreiben = ui.textarea("Anschreiben",
                                          value=row["anschreiben_body"]) \
                    .classes("w-full").props("autogrow")
                pdf_label = ui.label(
                    f"Mappe: {row['pdf_path']}" if row["pdf_path"]
                    else "No Mappe PDF yet — required before sending."
                ).classes("text-xs text-gray-600")

                async def save() -> bool:
                    values = {
                        "recipient": recipient.value.strip(),
                        "betreff": betreff.value.strip(),
                        "email_body": email_body.value,
                        "anschreiben_body": anschreiben.value,
                    }
                    if all(current[k] == v for k, v in values.items()):
                        return True
                    # The Mappe PDF embeds the letter text: editing the
                    # Anschreiben invalidates a previously built PDF.
                    clear_pdf = bool(
                        values["anschreiben_body"] != current["anschreiben_body"]
                        and current["pdf_path"]
                    )
                    updated = await run.io_bound(
                        _save_draft, job_id, values, clear_pdf
                    )
                    current.update(updated)
                    if clear_pdf:
                        pdf_label.set_text("No Mappe PDF yet — the letter "
                                           "changed; create it again.")
                        ui.notify("Letter changed — create the PDF again",
                                  type="warning")
                    return True

                async def save_only():
                    await save()
                    ui.notify("Saved", type="positive")
                    await refresh()

                async def make_pdf():
                    await save()
                    ui.notify("Creating Bewerbungsmappe…")
                    result = await mappe.create_mappe(job_id)
                    if not result["ok"]:
                        ui.notify(result["error"], type="warning",
                                  multi_line=True)
                        return
                    current["pdf_path"] = result["pdf_path"]
                    pdf_label.set_text(f"Mappe: {result['pdf_path']}")
                    size_mb = result["size_bytes"] / 1024 / 1024
                    ui.notify(f"Mappe ready: {result['pages']} pages, "
                              f"{size_mb:.1f} MB ✓", type="positive")
                    if result["warning"]:
                        ui.notify(result["warning"], type="warning",
                                  multi_line=True)

                def open_pdf():
                    path = current.get("pdf_path", "")
                    if not path:
                        ui.notify("create the Mappe first", type="warning")
                    elif not pathlib.Path(path).exists():
                        ui.notify("the Mappe file is gone — create it again",
                                  type="warning")
                    else:
                        open_in_system(path)

                async def send_now():
                    await save()
                    status = await run.io_bound(_send_status)
                    final, test_mode, error = send.resolve_recipient(
                        current["recipient"], {
                            "real_send_enabled":
                                "1" if status["real"] else "0",
                            "test_recipient": status["test_recipient"],
                        }
                    )
                    if error:
                        ui.notify(error, type="warning", multi_line=True)
                        return
                    mode = ("TEST send" if test_mode
                            else "REAL send to the company")
                    attachment = (pathlib.Path(current["pdf_path"]).name
                                  if current["pdf_path"] else "NONE")
                    with ui.dialog() as confirm, ui.card():
                        ui.label("Send this application?").classes("font-bold")
                        ui.label(f"{mode}: {final}").classes(
                            "text-sm font-bold text-red-700" if not test_mode
                            else "text-sm font-bold text-amber-700")
                        ui.label(f"Betreff: {current['betreff']}") \
                            .classes("text-sm")
                        ui.label(f"Attachment: {attachment}").classes("text-sm")
                        with ui.row().classes("justify-end gap-2 w-full"):
                            ui.button("Cancel",
                                      on_click=lambda: confirm.submit(False)) \
                                .props("flat")
                            ui.button("Send", icon="send",
                                      on_click=lambda: confirm.submit(True)) \
                                .props("color=positive")
                    confirm.open()
                    if not await confirm:
                        return
                    ui.notify("Sending…")
                    result = await send.send_draft(job_id)
                    if not result["ok"]:
                        ui.notify(result["error"], type="warning",
                                  multi_line=True)
                        await refresh()
                        return
                    ui.notify(
                        f"{'TEST sent' if result['test_mode'] else 'Sent'} "
                        f"to {result['recipient']} ✓", type="positive",
                    )
                    dialog.close()
                    await refresh()

                async def approve_from_editor():
                    await save()
                    result = await run.io_bound(send.approve, job_id)
                    if not result["ok"]:
                        ui.notify(result["error"], type="warning",
                                  multi_line=True)
                        return
                    ui.notify("Approved — auto-send will pick it up",
                              type="positive")
                    dialog.close()
                    await refresh()

                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Save", icon="save", on_click=save_only) \
                        .props("outline")
                    ui.button("Create PDF", icon="picture_as_pdf",
                              on_click=make_pdf).props("outline")
                    ui.button("Open PDF", icon="open_in_new",
                              on_click=open_pdf).props("outline")
                    if row["status"] == "ready":
                        ui.button("Approve for auto-send", icon="schedule_send",
                                  on_click=approve_from_editor).props("outline")
                    ui.button("Send now", icon="send", on_click=send_now) \
                        .props("color=positive")
                    ui.button("Close", on_click=dialog.close).props("flat")
            dialog.open()

        await refresh()
