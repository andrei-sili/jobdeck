"""Review queue: edit, approve and send drafted applications.

Every send is human-approved here (auto-send only transmits drafts the
user explicitly approved). The page is loud about the sending mode: while
real sending is OFF, the banner and the pre-send confirmation both show
the test recipient every message will actually go to.
"""

import logging

from nicegui import run, ui

from jobdeck import db
from jobdeck.dedupe import duplicates_for_jobs
from jobdeck.services import drafting, liveness, send
from jobdeck.ui import draft_editor, helpers, live
from jobdeck.ui.helpers import open_in_system, openable_url
from jobdeck.ui.layout import frame

log = logging.getLogger(__name__)

FILTERS = ["open", "sent", "discarded"]
FILTER_STATUSES = {
    # 'failed' belongs here too: its only other surface is the Job inbox's
    # Draft button, which disappears once the job leaves status 'new'.
    # 'generating' belongs here because the row EXISTS for the minute it takes
    # to write — and while no view listed it, pressing Draft a second time was
    # the only way to learn the app was working.
    "open": ["generating", "ready", "approved", "sending", "failed"],
    "sent": ["sent"],
    "discarded": ["discarded"],
}
EMPTY_TEXT = {
    "open": "No drafts waiting. Draft an application from the Job inbox first.",
    "sent": "Nothing sent yet.",
    "discarded": "No discarded drafts.",
}

# How often the queue re-reads itself while a draft is being written, so the
# row becomes the finished application on its own.
GENERATING_POLL_SECONDS = 5.0
# …and how many of those ticks to skip when nothing is being written. A gate
# derived only from the last render would never START polling for a draft begun
# in another tab or by the prepare-a-batch pass, and would never STOP for a
# claim stranded by a crash — so the slow heartbeat covers both, at one small
# query every half minute.
IDLE_POLL_EVERY = 6


def generating_line(updated_at: object) -> tuple[str, str]:
    """(text, CSS classes) for a draft that is being written right now.

    The cut-off is drafting's own, so this row and the Job inbox — two views
    of one database row — cannot contradict each other, and so this text can
    promise the restart exactly when the inbox's Draft button comes back."""
    if drafting.claim_is_stale(updated_at):
        minutes = int(drafting.claim_age_minutes(updated_at))
        return (f"⚠ Seit {minutes} Minuten kein Ergebnis — der Vorgang wurde "
                "abgebrochen. Im Job-Postfach erneut auf „Draft "
                "application“ drücken.", "text-sm text-amber-700")
    return ("Die Bewerbung wird gerade geschrieben — das dauert etwa eine "
            "Minute. Die Zeile aktualisiert sich von selbst.",
            "text-sm text-blue-700")


def _signature_of(con) -> tuple:
    """Everything this page states, cheaply comparable (see ui/live.py).

    Deliberately wider than the drafts: a row also carries the POSTING's
    liveness — the pre-send "the ad is gone" warning this page exists for — and
    the banner states the sending mode, which another tab can flip. A signature
    over the drafts alone left both able to go stale while the page sat open,
    which is precisely the window (liveness runs 90 s after every start, then he
    sends) that warning was written for."""
    # The pipeline first, the sending mode after it — the same order every
    # loader here reads in, and the reason the rule that pins it exists.
    signature = db.data_signature(con)
    return (signature, *sorted(db.send_mode(con).items()))


def _signature() -> tuple:
    with db.db() as con:
        return _signature_of(con)


def _load(filter_value: str) -> dict:
    """One read of everything the queue renders, signature included."""
    with db.db() as con:
        # First, before any row: see _load_jobs in jobs.py for why the order is
        # load-bearing.
        signature = _signature_of(con)
        rows = [dict(r) for r in
                db.list_drafts_with_jobs(con, FILTER_STATUSES[filter_value])]
        # Asked of the duplicate gate itself, for the drafts on screen: the
        # send path refuses a second application to a company (send.py), and
        # until now only the job inbox said so — on the FORM path, where he
        # drafts from the cockpit, nothing did.
        postings = [{"id": r["job_id"], "company": r["job_company"],
                     "contact_email": r["job_contact_email"]} for r in rows]
        applied = duplicates_for_jobs(con, postings)
        # A sent draft MADE the application at that company, so matching it
        # against itself is not a warning, it is an echo — every row of the
        # 'sent' tab would carry one. A second application at the same company
        # still warns, because then the match is a different row.
        own = {r["job_id"]: r["bewerbung_id"] for r in rows}
        applied = {job_id: match for job_id, match in applied.items()
                   if own.get(job_id) != match["id"]}
        return {
            "drafts": rows,
            "status": db.send_mode(con),
            "applied": applied,
            "signature": signature,
        }


@ui.page("/queue")
async def queue_page():
    async with frame("Review queue", current="bewerbungen"):
        filter_state = {"value": "open"}
        refresh_gen = {"n": 0}  # rapid filter flips: last request wins
        shown = {"live_claim": False}
        # Drafts he has expanded to read: a rebuild collapses every one of them.
        reading = {"rows": 0}
        applied: dict[int, dict] = {}

        banner = ui.row().classes("w-full items-center gap-2")
        # Beside the banner rather than inside it: refresh() clears `banner`.
        banner_extra = ui.row().classes("items-center")
        with ui.row().classes("items-center gap-4"):
            ui.toggle(FILTERS, value="open",
                      on_change=lambda e: set_filter(e.value))
        container = ui.column().classes("w-full gap-2")
        # Dialogs and post-await notifications live HERE, never in a row.
        # A handler runs in the slot of the button that fired it, and
        # refresh() clears `container` — since this page now refreshes on a
        # timer rather than only on a click, an editor open over the list
        # would otherwise be deleted mid-typing, and a coroutine parked on
        # `await confirm` would never resolve. `contents` keeps the host out
        # of the page's flex layout so it costs no blank row.
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
            view = await run.io_bound(_load, filter_state["value"])
            if gen != refresh_gen["n"]:
                return  # superseded — a newer refresh already owns the view
            drafts, status = view["drafts"], view["status"]
            applied.clear()
            applied.update(view["applied"])
            reading["rows"] = 0  # every expansion just died with the container
            live_view.mark(view["signature"])
            shown["live_claim"] = any(
                d["status"] == "generating"
                and not drafting.claim_is_stale(d["updated_at"]) for d in drafts)
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

        def track_reading(event) -> None:
            reading["rows"] = max(0, reading["rows"] + (1 if event.value else -1))

        # Fast while a draft is genuinely being written — that row has to turn
        # into the finished application by itself — and a slow heartbeat
        # otherwise, which is what covers a draft begun in another tab or by the
        # prepare-a-batch pass, and a claim stranded by a crash.
        # In the banner, at the top: an update notice under the list is one
        # nobody sees.
        with banner_extra:
            live_view = live.watch(
                _signature, refresh,
                seconds=GENERATING_POLL_SECONDS, idle_every=IDLE_POLL_EVERY,
                hot=lambda: shown["live_claim"],
                busy=lambda: reading["rows"] > 0 or live.dialog_open(),
            )

        def render_draft(row: dict):
            score = (f" · match {row['job_score']}"
                     if row["job_score"] is not None else "")
            # The head is the only part an unopened row shows, so a draft
            # being written has to say so THERE — 'generating' in the status
            # slot is the word the database uses, not the one he reads.
            state = ("wird geschrieben…" if row["status"] == "generating"
                     else row["status"])
            head = (f"{row['job_company']}  —  {row['job_title']}"
                    f"  ({state}{score})")
            with ui.expansion(head, on_value_change=track_reading) \
                    .classes("w-full border rounded"):
                if row["status"] == "generating":
                    ui.label(f"Begonnen {row['updated_at'][:16]}") \
                        .classes("text-xs text-gray-500")
                    text, classes = generating_line(row["updated_at"])
                    ui.label(text).classes(classes)
                    render_posting_link(row)
                    return
                ui.label(f"To: {row['recipient'] or '(no recipient yet)'} · "
                         f"updated {row['updated_at'][:16]}") \
                    .classes("text-xs text-gray-500")
                ui.label(row["betreff"]).classes("text-sm")
                if row["job_liveness"] == liveness.LIVENESS_GONE:
                    checked = (row["job_liveness_checked_at"] or "")[:10]
                    ui.label(f"⚠ Die Anzeige war am {checked} nicht mehr "
                             "online — vor dem Versand prüfen.") \
                        .classes("text-sm text-red-700")
                already = applied.get(row["job_id"])
                if already:
                    # The send path refuses this application anyway, inside its
                    # own claim. Saying so HERE is what stops him building a
                    # Mappe and pressing Send to be told no — two of the five
                    # drafts waiting in his queue were at such companies, and
                    # only the job inbox warned.
                    ui.label(helpers.applied_line(already)) \
                        .classes("text-sm text-amber-700")
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
                    if row["status"] == "failed":
                        ui.label("Drafting failed — write it again, or discard "
                                 "it.").classes("text-sm text-amber-700")
                        # It used to send him to the Job inbox for this, where
                        # the button only appears while the posting is still
                        # 'new' — so a failed draft for a posting he had already
                        # opened as a form had nowhere at all to be retried.
                        again = ui.button("Neu schreiben", icon="refresh") \
                            .props("outline")
                        again.on_click(lambda r=row, b=again: redraft(r, b))
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
                    render_posting_link(row)

        def render_posting_link(row: dict):
            """The one button every draft row offers, whatever its status."""
            posting_url = openable_url(row["job_url"])
            if posting_url:
                ui.button("Open posting", icon="open_in_new",
                          on_click=lambda u=posting_url:
                          ui.navigate.to(u, new_tab=True)) \
                    .props("flat")

        async def _simple_action(action, job_id: int, success: str):
            result = await run.io_bound(action, job_id)
            if not result["ok"]:
                say(result["error"], type="warning", multi_line=True)
            else:
                say(success, type="positive")
            await refresh()

        async def redraft(row: dict, button):
            """Write a failed draft again, from the screen it failed on."""
            say("Die Bewerbung wird geschrieben — das dauert etwa eine Minute…")
            button.set_text("wird geschrieben…")
            button.disable()
            try:
                result = await drafting.draft_for_job(row["job_id"])
            except Exception:  # noqa: BLE001 — one draft, not the page
                log.exception("re-drafting job %s raised", row["job_id"])
                result = {"ok": False,
                          "error": "Der Entwurf ist unerwartet fehlgeschlagen "
                                   "— Details stehen im Log."}
            await refresh()  # the row carries the outcome from here on
            if not result["ok"]:
                say(result["error"], type="warning", multi_line=True)
            else:
                say("Entwurf fertig ✓", type="positive")

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
                overlay.clear()
                with overlay, ui.dialog() as confirm, ui.card():
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
            """The shared editor, over this page's own overlay.

            One implementation, because it is the last screen before a message
            leaves: it resolves the recipient, shows what is about to go out and
            pins it with `expect=`. Stellen opens the very same one."""
            draft_editor.open_editor(
                row, overlay=overlay, say=say, on_change=refresh)

        await refresh()
