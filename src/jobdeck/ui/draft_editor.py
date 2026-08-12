"""The one place a draft is corrected, given its Mappe, and sent.

Extracted from the review queue when the Stellen screen needed the same thing.
It is SHARED rather than copied on purpose: this dialog resolves the recipient,
shows what is about to leave and pins it with `expect=` so the send cannot
describe one message and transmit another. Two copies of that is exactly how a
screen ends up telling him one thing while the gate below does another.

Nothing here decides whether a send is real — `services/send.py` does, inside
its own claim. This module only asks, shows and reports.
"""

import pathlib

from nicegui import run, ui

from jobdeck import db
from jobdeck.services import mappe, send
from jobdeck.ui import helpers
from jobdeck.ui.helpers import open_in_system

# What may still be edited. A draft that is sending or sent is the record of
# what actually went out, and a failed one has no text to correct — the way
# back from that is writing it again, not editing nothing.
EDITABLE_STATUS = ("ready", "approved")


def load(job_id: int) -> dict | None:
    """The draft for this posting, with the job fields the editor shows."""
    with db.db() as con:
        row = db.draft_with_job(con, job_id)
        return dict(row) if row is not None else None


def _current_send_status() -> dict:
    """The sending mode on its own connection, for the pre-send check."""
    with db.db() as con:
        return db.send_mode(con)


def _save_draft(job_id: int, values: dict, clear_pdf: bool):
    """Persist editor changes. Returns (draft, error).

    A dialog can stay open indefinitely while auto-send moves the draft on:
    writing then would silently rewrite the record of what actually went out,
    so an editable status is re-checked inside the write."""
    with db.db() as con:
        con.execute("BEGIN IMMEDIATE")
        current = db.get_draft_by_job(con, job_id)
        if current is None:
            return None, "the draft is gone — refresh the queue"
        if current["status"] not in EDITABLE_STATUS:
            return dict(current), (
                f"this draft is no longer editable (status: "
                f"{current['status']}) — your changes were not saved"
            )
        if clear_pdf:
            values = {**values, "pdf_path": ""}
        if current["status"] == "approved":
            # Approval is content-specific: auto-send must never transmit text
            # the user changed after approving it.
            values = {**values, "status": "ready"}
        db.upsert_draft(con, job_id, values)
        return dict(db.get_draft_by_job(con, job_id)), ""


def open_editor(row: dict, *, overlay, say, on_change,
                already_applied: dict | None = None) -> None:
    """Open the draft for this posting over whatever screen asked for it.

    `overlay` is the caller's own host — a sibling of its list, because a
    handler runs in the slot of the element that fired it and a refresh
    deletes that slot. `on_change` is the caller's refresh.
    """
    job_id = row["job_id"]
    current = dict(row)
    overlay.clear()
    with overlay, ui.dialog() as dialog, \
            ui.card().classes("w-[760px] max-w-full"):
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
            # The Mappe PDF renders both the letter text and the
            # Betreff: editing either invalidates a built PDF.
            clear_pdf = bool(current["pdf_path"] and (
                values["anschreiben_body"] != current["anschreiben_body"]
                or values["betreff"] != current["betreff"]
            ))
            was_approved = current["status"] == "approved"
            updated, error = await run.io_bound(
                _save_draft, job_id, values, clear_pdf
            )
            if updated is not None:
                current.update(updated)
            if error:
                say(error, type="warning", multi_line=True)
                await on_change()
                return False
            if clear_pdf:
                pdf_label.set_text("No Mappe PDF yet — the text "
                                   "changed; create it again.")
                say("Text changed — create the PDF again",
                          type="warning")
            if was_approved:
                say("Edited — the draft went back to ready; "
                          "approve it again for auto-send",
                          type="warning", multi_line=True)
            return True

        async def save_only():
            if not await save():
                return
            say("Saved", type="positive")
            await on_change()

        async def make_pdf():
            if not await save():
                return
            say("Creating Bewerbungsmappe…")
            result = await mappe.create_mappe(job_id)
            if not result["ok"]:
                say(result["error"], type="warning",
                          multi_line=True)
                return
            current["pdf_path"] = result["pdf_path"]
            pdf_label.set_text(f"Mappe: {result['pdf_path']}")
            say(helpers.mappe_summary(result), type="positive")
            if result["warning"]:
                say(result["warning"], type="warning",
                          multi_line=True)

        def open_pdf():
            path = current.get("pdf_path", "")
            if not path:
                say("create the Mappe first", type="warning")
            elif not pathlib.Path(path).exists():
                say("the Mappe file is gone — create it again",
                          type="warning")
            else:
                open_in_system(path)

        async def send_now():
            if not await save():
                return
            status = await run.io_bound(_current_send_status)
            final, test_mode, error = send.resolve_recipient(
                current["recipient"], {
                    "real_send_enabled":
                        "1" if status["real"] else "0",
                    "test_recipient": status["test_recipient"],
                }
            )
            if error:
                say(error, type="warning", multi_line=True)
                return
            mode = ("TEST send" if test_mode
                    else "REAL send to the company")
            attachment = (pathlib.Path(current["pdf_path"]).name
                          if current["pdf_path"] else "NONE")
            with overlay, ui.dialog() as confirm, ui.card():
                ui.label("Send this application?").classes("font-bold")
                ui.label(f"{mode}: {final}").classes(
                    "text-sm font-bold text-red-700" if not test_mode
                    else "text-sm font-bold text-amber-700")
                already = already_applied
                if already:
                    # last statement before the press; the gate inside
                    # the claim would refuse it a second later
                    ui.label(helpers.applied_line(already)) \
                        .classes("text-sm font-bold text-amber-700")
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
            say("Sending…")
            # Pin what the confirmation actually showed: between the
            # dialog and this call another tab could have edited the
            # draft or flipped the sending mode.
            result = await send.send_draft(job_id, expect={
                "updated_at": current["updated_at"],
                "recipient": current["recipient"],
                "betreff": current["betreff"],
                "email_body": current["email_body"],
                "anschreiben_body": current["anschreiben_body"],
                "pdf_path": current["pdf_path"],
                "test_mode": test_mode,
                "recipient_shown": final,
            })
            if not result["ok"]:
                say(result["error"], type="warning",
                          multi_line=True)
                await on_change()
                return
            say(
                f"{'TEST sent' if result['test_mode'] else 'Sent'} "
                f"to {result['recipient']} ✓", type="positive",
            )
            dialog.close()
            await on_change()

        async def approve_from_editor():
            if not await save():
                return
            result = await run.io_bound(send.approve, job_id)
            if not result["ok"]:
                say(result["error"], type="warning",
                          multi_line=True)
                return
            say("Approved — auto-send will pick it up",
                      type="positive")
            dialog.close()
            await on_change()

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
