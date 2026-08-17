"""Antworten: what came back, and what it did to the register.

Two piles and a state line. "Zu prüfen" is everything the machine would not
decide — LLM proposals, domain matches, ambiguous receipts — each one press
from settled. "Eingeordnet" is the ledger of what was decided, by the rules
or by him, each row honest about HOW its mail was tied to an application.
The state line says whether Gmail is being read at all, because the worst
version of this screen is one that looks quiet while the reader is broken.

Every excerpt and body on this page is HOSTILE input — mail anyone can send.
It is rendered exclusively through ui.label (NiceGUI escapes label text);
an AST test pins that no markdown/html sink ever appears in this module.
"""

import datetime

from nicegui import run, ui

from jobdeck import db, gmail
from jobdeck.services import register
from jobdeck.services import replies as reply_service
from jobdeck.ui import live
from jobdeck.ui.helpers import applied_line
from jobdeck.ui.layout import frame
from jobdeck.ui.rail import ANTWORTEN_PATH, EINSTELLUNGEN_PATH, clock

# How much of a mail the row shows before the "Ganze Mail" dialog takes over.
EXCERPT_CHARS = 220
LEDGER_LIMIT = 30

CLASS_LABELS = {
    "eingang": "Eingang",
    "absage": "Absage",
    "einladung": "Einladung",
    "sonstige": "Sonstiges",
    "auto": "Automatische Antwort",
    "": "Unklar",
}

# Same colour rule as the register: amber marks an open question, a rejection
# is a closed one and reads calmer.
_PILL = {"einladung": "ok", "eingang": "ok", "sonstige": "ok",
         "absage": "", "auto": "", "": "warn"}

# How the mail was tied to an application, in words he can check.
MATCHED_BY = {
    "thread": "über den Mail-Verlauf zugeordnet",
    "address": "über die Absenderadresse zugeordnet",
    "domain": "über die Absender-Domain zugeordnet",
    reply_service.MATCHED_RECEIPT: "Eingangsbestätigung zu einem Formular",
    reply_service.MATCHED_ATTACHED: "Eingangsbestätigung zu einem Formular",
}


def _signature(con) -> tuple:
    return (
        *db.data_signature(con),
        db.get_setting(con, reply_service.LAST_POLL_KEY, ""),
        db.get_setting(con, reply_service.LAST_ERROR_KEY, ""),
        gmail.can_read(),
    )


def signature() -> tuple:
    with db.db() as con:
        return _signature(con)


def _load() -> dict:
    with db.db() as con:
        sig = _signature(con)
        return {
            "signature": sig,
            "pending": [dict(row) for row in db.pending_review_replies(con)],
            "settled": [dict(row)
                        for row in db.list_inbound_replies(con, LEDGER_LIMIT)],
            "last_poll": db.get_setting(con, reply_service.LAST_POLL_KEY, ""),
            "last_error": db.get_setting(con, reply_service.LAST_ERROR_KEY, ""),
            "ai_on": (db.ai_enabled(con) and db.get_setting(
                con, reply_service.AI_TOGGLE_KEY, "0") == "1"),
            "connected": gmail.is_connected(),
            "can_read": gmail.can_read(),
        }


def _when(row: dict) -> str:
    return clock(row.get("internal_date") or row.get("created_at") or "")


def _sender_line(row: dict) -> str:
    firma = (row.get("bewerbung_firma") or row.get("job_company")
             or row.get("from_addr") or "")
    origin = MATCHED_BY.get(row.get("matched_by") or "", "")
    parts = [part for part in (firma, origin, _when(row)) if part]
    return " · ".join(parts)


@ui.page(ANTWORTEN_PATH)
async def antworten_page():
    async with frame("Antworten", current="antworten"):
        refresh_gen = {"n": 0}

        header = ui.row().classes("w-full items-center gap-3")
        container = ui.column().classes("w-full gap-4")
        # Dialogs and post-await messages live in a sibling of the container,
        # never in the row that opened them: refresh() clears `container` and
        # this page refreshes on the watcher's timer, so anything parented in
        # a row would be raised into a deleted slot.
        overlay = ui.column().classes("contents")

        def say(message: str, **kwargs) -> None:
            with overlay:
                ui.notify(message, **kwargs)

        # ------------------------------------------------------------------
        # The state line: is anything being read at all?
        # ------------------------------------------------------------------
        def draw_state(view: dict) -> None:
            with ui.column().classes("jd-card gap-2"):
                ui.label("Gmail liest mit, du entscheidest") \
                    .classes("jd-card-title")
                if not view["connected"]:
                    ui.label("Gmail ist nicht verbunden — ohne Verbindung "
                             "kann hier nichts ankommen.").classes("jd-reason")
                    ui.button("Zu den Einstellungen",
                              on_click=lambda: ui.navigate.to(
                                  EINSTELLUNGEN_PATH)).props("outline no-caps")
                    return
                if not view["can_read"]:
                    ui.label("Die Verbindung kann senden, aber noch nicht "
                             "lesen. Einmal neu verbinden erteilt den "
                             "Lese-Zugriff (gmail.modify).") \
                        .classes("jd-reason")
                    ui.button("Zu den Einstellungen",
                              on_click=lambda: ui.navigate.to(
                                  EINSTELLUNGEN_PATH)).props("outline no-caps")
                    return
                last = clock(view["last_poll"])
                ui.label("Alle 10 Minuten wird der Posteingang gelesen — "
                         + (f"zuletzt {last}." if last
                            else "der erste Lauf steht noch aus.")) \
                    .classes("jd-card-sub")
                if view["last_error"]:
                    ui.label(f"⚠ Letzter Lauf: {view['last_error']}") \
                        .classes("jd-reason")
                # What is automatic, stated out loud: the tiering decision
                # (ROADMAP 2026-08-05) supersedes the mockup's blanket
                # "Nichts ändert den Status ohne dich".
                ui.label("Eindeutige Absagen und Einladungen im Mail-Verlauf "
                         "einer Bewerbung trägt JobDeck selbst ein, ebenso "
                         "Eingangsbestätigungen aus der Domain der Anzeige. "
                         "Jede Zeile unten sagt, ob sie automatisch kam, und "
                         "ein Klick korrigiert sie. Alles andere wartet hier "
                         "auf dich.").classes("jd-card-sub")
                if not view["ai_on"]:
                    ui.label("Unklare Antworten bleiben unklar: die "
                             "KI-Einordnung ist aus (Einstellungen → AI).") \
                        .classes("jd-card-sub")

        # ------------------------------------------------------------------
        # Zu prüfen
        # ------------------------------------------------------------------
        def draw_pending(view: dict) -> None:
            pending = view["pending"]
            with ui.column().classes("jd-card gap-3"):
                ui.label("Zu prüfen").classes("jd-card-title")
                if not pending:
                    ui.label("Nichts wartet auf dein Urteil.") \
                        .classes("jd-card-sub")
                    return
                ui.label(register.plural(
                    len(pending), "Antwort wartet auf dein Urteil",
                    "Antworten warten auf dein Urteil")) \
                    .classes("jd-card-sub")
                for row in pending:
                    _pending_row(row)

        def _pending_row(row: dict) -> None:
            with ui.column().classes("w-full gap-1 jd-reply-row"):
                ui.label(_sender_line(row)).classes("jd-card-sub")
                ui.label(row.get("subject") or "(kein Betreff)") \
                    .classes("font-medium")
                excerpt = " ".join(
                    (row.get("body_text") or row.get("snippet") or "").split())
                if excerpt:
                    ui.label(excerpt[:EXCERPT_CHARS]
                             + ("…" if len(excerpt) > EXCERPT_CHARS else ""))
                proposed = row.get("classification") or ""
                if proposed and row.get("classified_by") == "llm":
                    ui.label(f"Vorschlag der KI: "
                             f"{CLASS_LABELS.get(proposed, proposed)}?") \
                        .classes("jd-reason")
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    if _is_receipt_proposal(row):
                        ui.button(
                            "Als Bewerbung eintragen",
                            on_click=lambda _=None, r=row["id"]: adopt(r)) \
                            .props("unelevated no-caps")
                    else:
                        for value in ("absage", "einladung", "eingang",
                                      "sonstige"):
                            ui.button(
                                CLASS_LABELS[value],
                                on_click=lambda _=None, r=row["id"],
                                v=value: classify(r, v)) \
                                .props(("outline" if value != proposed
                                        else "unelevated") + " no-caps")
                    ui.button("Ganze Mail",
                              on_click=lambda _=None, r=dict(row):
                              show_mail(r)).props("flat no-caps")
                    ui.button("Keiner Bewerbung zuordnen",
                              on_click=lambda _=None, r=row["id"]:
                              dismiss(r)).props("flat no-caps")

        def _is_receipt_proposal(row: dict) -> bool:
            return (row.get("matched_by") == reply_service.MATCHED_RECEIPT
                    and row.get("job_id") is not None
                    and row.get("bewerbung_id") is None)

        # ------------------------------------------------------------------
        # Eingeordnet
        # ------------------------------------------------------------------
        def draw_settled(view: dict) -> None:
            settled = view["settled"]
            with ui.column().classes("jd-card gap-3"):
                ui.label("Eingeordnet").classes("jd-card-title")
                if not settled:
                    ui.label("Noch keine Antwort gelesen.") \
                        .classes("jd-card-sub")
                    return
                for row in settled:
                    _settled_row(row)

        def _settled_row(row: dict) -> None:
            classification = row.get("classification") or ""
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.label(_when(row)).classes("jd-mono")
                ui.label(row.get("bewerbung_firma") or row.get("job_company")
                         or row.get("from_addr") or "") \
                    .classes("font-medium") \
                    .style("overflow:hidden;text-overflow:ellipsis;"
                           "white-space:nowrap")
                with ui.element("span").classes(
                        f"jd-pill {_PILL.get(classification, '')}"):
                    ui.label(CLASS_LABELS.get(classification, classification))
                ui.label("bestätigt" if row.get("classified_by")
                         == "reply_manual" else "automatisch") \
                    .classes("jd-card-sub")
                ui.space()
                # Only a row this app CREATED may be taken back out; a
                # receipt attached to an application he recorded by hand
                # offers no undo, because the ledger row is not ours.
                if (row.get("matched_by") == reply_service.MATCHED_RECEIPT
                        and row.get("bewerbung_id") is not None):
                    ui.button("Rückgängig",
                              on_click=lambda _=None, r=row["id"]:
                              undo(r)).props("flat dense no-caps")
                elif row.get("bewerbung_id") is not None:
                    # The card above promises "ein Klick korrigiert sie".
                    # Without this the promise was false for every row the
                    # rules filed by themselves — the only ones it is about.
                    ui.button("Korrigieren",
                              on_click=lambda _=None, r=dict(row):
                              correct(r)).props("flat dense no-caps")
                ui.button("Ganze Mail",
                          on_click=lambda _=None, r=dict(row): show_mail(r)) \
                    .props("flat dense no-caps")

        def correct(row: dict) -> None:
            """Re-classify a settled row. His verdict outranks the reader's:
            `resolve_review` writes with source 'reply_manual', which the
            anti-downgrade rank does not apply to."""
            current = row.get("classification") or ""
            with overlay, ui.dialog() as dialog, ui.card():
                ui.label("Wie war diese Antwort gemeint?") \
                    .classes("font-medium")
                ui.label(_sender_line(row)).classes("jd-card-sub")
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    for value in ("absage", "einladung", "eingang",
                                  "sonstige"):
                        async def pick(_=None, v=value):
                            dialog.close()
                            await classify(row["id"], v)
                        ui.button(CLASS_LABELS[value], on_click=pick) \
                            .props(("unelevated" if value == current
                                    else "outline") + " no-caps")
                ui.button("Abbrechen", on_click=dialog.close) \
                    .props("flat no-caps")
            dialog.open()

        # ------------------------------------------------------------------
        # The full mail, in a dialog — label-only, hostile text stays text
        # ------------------------------------------------------------------
        def show_mail(row: dict) -> None:
            with overlay, ui.dialog() as dialog, ui.card().classes(
                    "w-[44rem] max-w-full"):
                ui.label(row.get("subject") or "(kein Betreff)") \
                    .classes("font-medium")
                ui.label(_sender_line(row)).classes("jd-card-sub")
                body = row.get("body_text") or row.get("snippet") or ""
                ui.label(body or "(kein Text gespeichert)") \
                    .style("white-space: pre-wrap") \
                    .classes("text-sm")
                ui.button("Schließen", on_click=dialog.close).props("flat no-caps")
            dialog.open()

        # ------------------------------------------------------------------
        # Actions
        # ------------------------------------------------------------------
        async def classify(row_id: int, classification: str) -> None:
            ok = await run.io_bound(
                reply_service.resolve_review, row_id, classification)
            if ok:
                say(f"Eingeordnet als "
                    f"{CLASS_LABELS.get(classification, classification)} — "
                    f"der Status der Bewerbung ist nachgezogen.",
                    type="positive")
            else:
                say("Diese Zeile gibt es nicht mehr.", type="warning")
            await refresh()

        async def adopt(row_id: int) -> None:
            outcome = await run.io_bound(reply_service.adopt_receipt, row_id)
            if outcome.get("ok"):
                say("Bewerbung eingetragen — sie steht jetzt im Register.",
                    type="positive")
            elif outcome.get("duplicate") is not None:
                say(applied_line(outcome["duplicate"]), type="warning")
            else:
                say("Eintragen ging nicht — die Anzeige fehlt.",
                    type="warning")
            await refresh()

        async def dismiss(row_id: int) -> None:
            await run.io_bound(reply_service.dismiss_review, row_id)
            say("Abgelegt — keiner Bewerbung zugeordnet.")
            await refresh()

        async def undo(row_id: int) -> None:
            ok = await run.io_bound(reply_service.undo_receipt, row_id)
            say("Bewerbung wieder ausgetragen — die Mail wartet unter "
                "„Zu prüfen“." if ok
                else "Da war nichts mehr rückgängig zu machen.",
                type="positive" if ok else "warning")
            await refresh()

        async def refresh() -> None:
            # Last request wins: a click's refresh and the watcher's tick can
            # be in flight together, and the older result finishing second
            # would repaint the page with rows from before the write.
            refresh_gen["n"] += 1
            generation = refresh_gen["n"]
            view = await run.io_bound(_load)
            if view is None or generation != refresh_gen["n"]:
                return
            live_view.mark(view["signature"])
            container.clear()
            with container:
                draw_state(view)
                draw_pending(view)
                draw_settled(view)

        with header:
            ui.label(datetime.date.today().strftime("%d.%m.%Y")) \
                .classes("jd-card-sub")
            ui.space()
            live_host = ui.row().classes("items-center")

        with live_host:
            live_view = live.watch(signature, refresh, busy=live.dialog_open)

        await refresh()
