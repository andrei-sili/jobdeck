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
from jobdeck.dedupe import duplicates_for_jobs
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


def applied_at_this_company(job_id: int) -> dict | None:
    """The application that already went to this posting's company, or None.

    Asked HERE, of the database, at the moment the confirmation is built. It
    used to be handed in by the caller — first as a snapshot taken when the
    editor opened, then as a callable over the caller's cached dict — and both
    were stale by construction: the page cannot refresh while a dialog is open,
    which is exactly the window an auto-send tick or a second tab uses. The
    gate inside the claim reads the same table; this is so he sees it first."""
    with db.db() as con:
        job = db.get_job(con, job_id)
        if job is None:
            return None
        return duplicates_for_jobs(con, [dict(job)]).get(job_id)


def open_editor(row: dict, *, overlay, say, on_change) -> None:
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
        recipient = ui.input(f"Empfänger ({hint})",
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
            else "Noch keine Bewerbungsmappe — ohne sie geht nichts raus."
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
                pdf_label.set_text("Noch keine Bewerbungsmappe — der Text "
                                   "changed; create it again.")
                say("Text geändert — die Mappe neu bauen",
                          type="warning")
            if was_approved:
                say("Geändert — der Entwurf ist wieder offen; "
                          "approve it again for auto-send",
                          type="warning", multi_line=True)
            return True

        async def save_only():
            if not await save():
                return
            say("Gespeichert", type="positive")
            await on_change()

        async def make_pdf():
            if not await save():
                return
            say("Bewerbungsmappe wird gebaut…")
            result = await mappe.create_mappe(job_id)
            if not result["ok"]:
                say(result["error"], type="warning",
                          multi_line=True)
                return
            # `mappe` finishes with its own upsert_draft, and every upsert
            # rewrites `updated_at` — so the snapshot this dialog pins with
            # `expect=` went stale the moment the PDF was built, and the send
            # that followed was refused with "the draft changed since you
            # reviewed it". Every time, on the path this screen is built
            # around, turning the last human gate before a real e-mail into a
            # dialog he learns to dismiss and press again.
            fresh = await run.io_bound(load, job_id)
            if fresh is not None:
                current.update(fresh)
            else:
                current["pdf_path"] = result["pdf_path"]
            pdf_label.set_text(f"Mappe: {current['pdf_path']}")
            say(helpers.mappe_summary(result), type="positive")
            if result["warning"]:
                say(result["warning"], type="warning",
                          multi_line=True)

        def open_pdf():
            path = current.get("pdf_path", "")
            if not path:
                say("Erst die Bewerbungsmappe bauen", type="warning")
            elif not pathlib.Path(path).exists():
                say("Die Mappe-Datei ist weg — neu bauen",
                          type="warning")
            else:
                open_in_system(path)

        async def send_now():
            if not await save():
                return
            # The draft can move while this dialog sits open — auto-send picks
            # it up, another tab sends it. `save()` returns early when nothing
            # was typed, so without this the confirmation would state "REAL
            # send to the company" for a message the claim then refuses.
            latest = await run.io_bound(load, job_id)
            if latest is None or latest["status"] not in EDITABLE_STATUS:
                say(f"Dieser Entwurf ist nicht mehr sendbar (Status: "
                    f"{latest['status'] if latest else 'weg'}) — "
                    f"im Postausgang auflösen.",
                    type="warning", multi_line=True)
                await on_change()
                return
            current.update(latest)
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
            # The last line he reads before a message leaves. It was English
            # on a German screen — and this is the one sentence in the app
            # where the difference between a test and a real employer is
            # stated, so it is the last place that should need translating.
            mode = ("Testversand an" if test_mode
                    else "ECHTER Versand an die Firma")
            attachment = (pathlib.Path(current["pdf_path"]).name
                          if current["pdf_path"] else "keiner")
            already = await run.io_bound(applied_at_this_company, job_id)
            with overlay, ui.dialog() as confirm, ui.card():
                ui.label("Diese Bewerbung abschicken?").classes("font-bold")
                ui.label(f"{mode}: {final}").classes(
                    "text-sm font-bold text-red-700" if not test_mode
                    else "text-sm font-bold text-amber-700")
                # Read HERE rather than when the dialog opened: an application
                # to this company can land while it sits open (an auto-send
                # tick, a second tab), and this is the last statement before
                # the press. The gate inside the claim would refuse it a second
                # later, but the point of this line is that he sees it first.
                if already:
                    ui.label(helpers.applied_line(already)) \
                        .classes("text-sm font-bold text-amber-700")
                ui.label(f"Betreff: {current['betreff']}") \
                    .classes("text-sm")
                ui.label(f"Anhang: {attachment}").classes("text-sm")
                with ui.row().classes("justify-end gap-2 w-full"):
                    ui.button("Abbrechen",
                              on_click=lambda: confirm.submit(False)) \
                        .props("flat")
                    ui.button("Abschicken", icon="send",
                              on_click=lambda: confirm.submit(True)) \
                        .props("color=positive")
            confirm.open()
            if not await confirm:
                return
            say("Wird gesendet…")
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
            say("Freigegeben — der Auto-Versand übernimmt sie.",
                      type="positive")
            dialog.close()
            await on_change()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Speichern", icon="save", on_click=save_only) \
                .props("outline")
            ui.button("Mappe bauen", icon="picture_as_pdf",
                      on_click=make_pdf).props("outline")
            ui.button("Mappe öffnen", icon="open_in_new",
                      on_click=open_pdf).props("outline")
            if row["status"] == "ready":
                ui.button("Für Auto-Versand freigeben", icon="schedule_send",
                          on_click=approve_from_editor).props("outline")
            if row["status"] in EDITABLE_STATUS:
                # A draft that is sending or sent is the record of what went
                # out. Offering "Send now" on one made the pre-send
                # confirmation state "REAL send to the company" for a message
                # the service refuses inside its own claim — and that dialog's
                # whole job is to be trustworthy.
                ui.button("Jetzt senden", icon="send", on_click=send_now) \
                    .props("color=positive")
            ui.button("Close", on_click=dialog.close).props("flat")
    dialog.open()
