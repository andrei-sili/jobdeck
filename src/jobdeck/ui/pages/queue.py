"""Postausgang: the letters written and not yet sent.

The second face of the Bewerbungen rubric — the stack that drains to zero,
beside the register that only grows. Every send is human-approved here
(auto-send only transmits drafts the user explicitly approved). The page is
loud about the sending mode: while real sending is OFF, the banner and the
pre-send confirmation both show the test recipient every message will
actually go to.
"""

import logging

from nicegui import run, ui

from jobdeck import db
from jobdeck.dedupe import duplicates_for_jobs
from jobdeck.services import drafting, liveness, send
from jobdeck.ui import draft_editor, helpers, live
from jobdeck.ui.helpers import open_in_system, openable_url
from jobdeck.ui.layout import BEWERBUNGEN_TABS, frame, tabs

log = logging.getLogger(__name__)

# The stack, what has gone, and what was put away. German like the rest of
# the app: this screen sat in English long after the rubric around it changed
# language, and it is the last one a message passes through.
# Three nouns, one word class. "Warten" asserted of five states what is true
# of one (a failed draft is not waiting), and "Raus" was markedly colloquial
# for the tab that lists what has gone to employers.
FILTERS = {"open": "Offen", "sent": "Erledigt", "discarded": "Verworfen"}
FILTER_STATUSES = {
    # 'failed' belongs here too: its only other surface is the Job inbox's
    # Draft button, which disappears once the job leaves status 'new'.
    # 'generating' belongs here because the row EXISTS for the minute it takes
    # to write — and while no view listed it, pressing Draft a second time was
    # the only way to learn the app was working.
    "open": ["generating", "ready", "approved", "sending", "failed"],
    # 'filed' belongs beside 'sent' and nowhere else: both letters are with an
    # employer, and the only difference is that this one travelled inside a
    # Mappe he uploaded rather than in an e-mail this app addressed.
    "sent": ["sent", "filed"],
    "discarded": ["discarded"],
}
EMPTY_TEXT = {
    "open": "Nichts offen. Ein Anschreiben entsteht unter „Stellen“.",
    "sent": "Bisher ist nichts rausgegangen.",
    "discarded": "Nichts verworfen.",
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


# The head of a row is the only part an unopened one shows, so it has to be
# read rather than decoded: 'ready' and 'filed' are the words the database
# uses. A status this map has not been taught falls through to its own name,
# which is ugly on purpose — a silent blank would hide a state entirely.
DRAFT_STATE = {
    "generating": "wird geschrieben…",
    "ready": "wartet",
    "approved": "freigegeben",
    "sending": "wird gesendet…",
    "sent": "gesendet",
    # NOT "eingereicht": that word claims an employer received this text, and
    # 'filed' only knows that an application for the posting is in the ledger.
    # The register says which, because it holds the row that can tell.
    "filed": "abgelegt",
    "failed": "fehlgeschlagen",
    "discarded": "verworfen",
}


def draft_state(status: object) -> str:
    """The German word for a draft's state, for the one line he reads."""
    return DRAFT_STATE.get(str(status or ""), str(status or ""))


def generating_line(updated_at: object) -> tuple[str, str]:
    """(text, CSS classes) for a draft that is being written right now.

    The cut-off is drafting's own, so this row and the Job inbox — two views
    of one database row — cannot contradict each other, and so this text can
    promise the restart exactly when the inbox's Draft button comes back."""
    if drafting.claim_is_stale(updated_at):
        minutes = int(drafting.claim_age_minutes(updated_at))
        return (f"⚠ Seit {minutes} Minuten kein Ergebnis — der Vorgang wurde "
                "abgebrochen. Unter „Stellen“ lässt sich das Anschreiben "
                "neu schreiben.", "text-sm text-amber-700")
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
        # current send path refuses a second application to a company
        # (send.py). The accepted identity policy is documented in ADR 0002;
        # this warning reflects current behavior until that policy is built.
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
    async with frame("Postausgang", current="bewerbungen", shelf=False):
        filter_state = {"value": "open"}
        refresh_gen = {"n": 0}  # rapid filter flips: last request wins
        shown = {"live_claim": False}
        # Drafts he has expanded to read: a rebuild collapses every one of them.
        reading = {"rows": 0}
        applied: dict[int, dict] = {}

        tabs("postausgang", BEWERBUNGEN_TABS)
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
                    ui.label("Echter Versand ist an — die E-Mails gehen an "
                             "die Firmen.").classes(
                        "text-sm font-bold text-red-700")
                else:
                    target = status["test_recipient"] or \
                        "niemanden — in den Einstellungen eine Testadresse " \
                        "eintragen"
                    ui.label(f"Testmodus — jeder Versand geht an: {target}") \
                        .classes("text-sm font-bold text-amber-700")
                ui.label(f"{status['sent_today']} von {status['cap']} heute "
                         "gesendet").classes("text-xs text-gray-500")
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
            head = (f"{row['job_company']}  —  {row['job_title']}"
                    f"  ({draft_state(row['status'])}{score})")
            with ui.expansion(head, on_value_change=track_reading) \
                    .classes("w-full border rounded"):
                if row["status"] == "generating":
                    ui.label(f"Begonnen {row['updated_at'][:16]}") \
                        .classes("text-xs text-gray-500")
                    text, classes = generating_line(row["updated_at"])
                    ui.label(text).classes(classes)
                    render_posting_link(row)
                    return
                ui.label(f"An: {row['recipient'] or '(noch kein Empfänger)'} "
                         f"· geändert {row['updated_at'][:16]}") \
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
                    ui.label("Noch keine Bewerbungsmappe — ohne sie geht "
                             "nichts raus.").classes("text-xs text-amber-700")
                if row["error"]:
                    ui.label(row["error"]).classes("text-sm text-red-700")
                with ui.row().classes("gap-2"):
                    if row["status"] in ("ready", "approved"):
                        ui.button("Prüfen und senden", icon="edit_note",
                                  on_click=lambda r=row: show_editor(r)) \
                            .props("outline")
                        if row["status"] == "ready":
                            ui.button("Für Auto-Versand freigeben",
                                      icon="schedule_send",
                                      on_click=lambda r=row: approve(r)) \
                                .props("outline")
                        else:
                            ui.button("Freigabe zurücknehmen", icon="undo",
                                      on_click=lambda r=row: unapprove(r)) \
                                .props("outline")
                        ui.button("Verwerfen", icon="delete",
                                  on_click=lambda r=row: discard(r)) \
                            .props("outline color=grey")
                    if row["status"] == "failed":
                        ui.label("Der Entwurf ist fehlgeschlagen — neu "
                                 "schreiben oder verwerfen.") \
                            .classes("text-sm text-amber-700")
                        # It used to send him to the Job inbox for this, where
                        # the button only appears while the posting is still
                        # 'new' — so a failed draft for a posting he had already
                        # opened as a form had nowhere at all to be retried.
                        again = ui.button("Neu schreiben", icon="refresh") \
                            .props("outline")
                        again.on_click(lambda r=row, b=again: redraft(r, b))
                        ui.button("Verwerfen", icon="delete",
                                  on_click=lambda r=row: discard(r)) \
                            .props("outline color=grey")
                    if row["status"] == "sending":
                        ui.label("Ein Versand läuft — oder er ist stecken "
                                 "geblieben. Vorher im Gmail-Ordner "
                                 "„Gesendet“ nachsehen. Ein steckengebliebener "
                                 "Testversand gilt immer als „nicht "
                                 "gesendet“.") \
                            .classes("text-sm text-amber-700")
                        ui.button("Nicht gesendet — zurücklegen", icon="undo",
                                  on_click=lambda r=row: resolve(r, False)) \
                            .props("outline")
                        ui.button("Doch gesendet — eintragen", icon="check",
                                  on_click=lambda r=row: resolve(r, True)) \
                            .props("outline color=positive")
                    if row["status"] in ("sent", "filed"):
                        # Which hand carried it. "sent" beside a letter that
                        # went into an employer's upload field would credit
                        # this app with an e-mail it never addressed.
                        sent_info = (
                            f"zur eingetragenen Bewerbung abgelegt "
                            f"{row['updated_at'][:16]}"
                            if row["status"] == "filed"
                            else f"gesendet {row['updated_at'][:16]}")
                        if row["gmail_message_id"]:
                            sent_info += f" · Gmail-Kennung {row['gmail_message_id']}"
                        ui.label(sent_info).classes("text-xs text-gray-500")
                        if row["pdf_path"]:
                            ui.button("Mappe öffnen", icon="open_in_new",
                                      on_click=lambda r=row:
                                      open_in_system(r["pdf_path"])) \
                                .props("outline")
                    if row["status"] == "discarded":
                        ui.button("Zurückholen", icon="restore",
                                  on_click=lambda r=row: restore(r)) \
                            .props("outline")
                    render_posting_link(row)

        def render_posting_link(row: dict):
            """The one button every draft row offers, whatever its status."""
            posting_url = openable_url(row["job_url"])
            if posting_url:
                ui.button("Anzeige öffnen", icon="open_in_new",
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
                                 "Freigegeben — der Auto-Versand übernimmt sie.")

        async def unapprove(row: dict):
            await _simple_action(send.unapprove, row["job_id"],
                                 "Freigabe zurückgenommen")

        async def discard(row: dict):
            await _simple_action(send.discard, row["job_id"], "Verworfen")

        async def restore(row: dict):
            await _simple_action(send.restore, row["job_id"], "Zurückgeholt")

        async def resolve(row: dict, assume_sent: bool):
            if assume_sent:
                overlay.clear()
                with overlay, ui.dialog() as confirm, ui.card():
                    ui.label("Als gesendet eintragen?").classes("font-bold")
                    ui.label("Nur wenn der Gmail-Ordner „Gesendet“ diese "
                             "Nachricht zeigt. Die Bewerbung wird dann ohne "
                             "Gmail-Kennung eingetragen.").classes("text-sm")
                    with ui.row().classes("justify-end gap-2 w-full"):
                        ui.button("Abbrechen",
                                  on_click=lambda: confirm.submit(False)) \
                            .props("flat")
                        ui.button("Eintragen",
                                  on_click=lambda: confirm.submit(True)) \
                            .props("color=positive")
                confirm.open()
                if not await confirm:
                    return
            await _simple_action(
                lambda job_id: send.resolve_sending(job_id, assume_sent),
                row["job_id"],
                "Als gesendet eingetragen" if assume_sent
                else "Zurückgelegt",
            )

        def show_editor(row: dict):
            """The shared editor, over this page's own overlay.

            One implementation, because it is the last screen before a message
            leaves: it resolves the recipient, shows what is about to go out and
            pins it with `expect=`. Stellen opens the very same one."""
            draft_editor.open_editor(
                row, overlay=overlay, say=say, on_change=refresh)

        await refresh()
