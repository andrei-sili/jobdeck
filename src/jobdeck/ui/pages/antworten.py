"""Antworten: what came back, and what it did to the register.

A list and a reading panel, the shape Stellen already proved. A row is a
VORGANG — one application with every mail that arrived for it — not a mail:
42 waiting mails touch 30 applications, and seven employers hold several, so
a mail-per-card shelf scattered one firm's four replies across four unrelated
positions in a 7 000 px column.

Two current product rules protect application state:
a verdict may raise an application's status on one press but never lower it
— 23 of those 42 hang off applications already closed — and the closed ones
are a VIEW rather than a pile, clearable in one gesture that writes no status
at all.

Every excerpt and body on this page is HOSTILE input — mail anyone can send.
It is rendered exclusively through ui.label (NiceGUI escapes label text);
an AST test pins that no markdown/html sink ever appears in this module.
"""

import dataclasses

from nicegui import run, ui

from jobdeck import db, gmail, replies
from jobdeck import settings as app_settings
from jobdeck.constants import CLASSIFICATION_TO_STATUS, OFFENE_STATUS, STATUS_RANK
from jobdeck.services import register
from jobdeck.services import replies as reply_service
from jobdeck.ui import live
from jobdeck.ui.helpers import hold_line
from jobdeck.ui.layout import frame
from jobdeck.ui.rail import ANTWORTEN_PATH, EINSTELLUNGEN_PATH, clock

# The reader shows the whole mail; past this it says where it cut, the way
# Stellen does for a posting.
MAIL_LIMIT = 40_000
LEDGER_LIMIT = 60

CLASS_LABELS = {
    "eingang": "Eingang",
    "absage": "Absage",
    "einladung": "Einladung",
    "sonstige": "Sonstiges",
    "auto": "Automatische Antwort",
    "": "Unklar",
}

# Fixed order, so the same verdict is always under the same finger.
VERDICTS = ("absage", "einladung", "eingang", "sonstige")

# Same colour rule as the register: amber marks an open question, a rejection
# is a closed one and reads calmer.
_PILL = {"einladung": "ok", "eingang": "ok", "sonstige": "ok",
         "absage": "", "auto": "", "": "warn"}

# How the mail was tied to an application, in words he can check.
MATCHED_BY = {
    "thread": "über den Mail-Verlauf zugeordnet",
    "address": "über die Absenderadresse zugeordnet",
    "domain": "über die Absender-Domain zugeordnet",
    "name": "über den Firmennamen zugeordnet",
    reply_service.MATCHED_RECEIPT: "Eingangsbestätigung zu einem Formular",
    reply_service.MATCHED_ATTACHED: "Eingangsbestätigung zu einem Formular",
}
# The row is one nowrap line with an ellipsis, so the origin has to be a word
# rather than a sentence; the sentence stays in the reader.
MATCHED_SHORT = {
    "thread": "Mail-Verlauf",
    "address": "Absenderadresse",
    "domain": "Absender-Domain",
    "name": "Firmenname",
    reply_service.MATCHED_RECEIPT: "Eingangsbestätigung",
    reply_service.MATCHED_ATTACHED: "Eingangsbestätigung",
}


@dataclasses.dataclass(frozen=True)
class View:
    key: str
    label: str
    empty: str


VIEWS = (
    View("offen", "Offene Bewerbungen",
         "Keine Antwort wartet auf dein Urteil."),
    View("abgeschlossen", "Abgeschlossene Bewerbungen",
         "Keine offenen Mails zu abgeschlossenen Bewerbungen."),
    View("alle", "Alle zu prüfen", "Nichts wartet auf dein Urteil."),
    View("eingeordnet", "Eingeordnet",
         "Die eingeordneten Antworten stehen rechts."),
)
DEFAULT_VIEW = "offen"
_VIEW_KEYS = {view.key for view in VIEWS}


# --------------------------------------------------------------------------
# Pure shaping — the part worth testing without a browser
# --------------------------------------------------------------------------
def is_closed(status: str) -> bool:
    """An application nothing on this screen can usefully change.

    An empty status is a mail with no application at all (a receipt proposal),
    which is the most open thing there is, not the most closed."""
    return bool(status) and status not in OFFENE_STATUS


def kept_status(current: str, classification: str) -> str:
    """The status a FIRST press would leave standing, or '' when it may write.

    The mirror of the guard in `replies.resolve_review`, so a button can state
    its consequence BEFORE it is pressed rather than after. Two copies of one
    rule is exactly the drift this project keeps paying for, so a test pins
    them equal over every status/verdict pair."""
    wanted = CLASSIFICATION_TO_STATUS.get(classification, "")
    if not wanted or not current or current == wanted:
        return ""
    if STATUS_RANK.get(wanted, 0) <= STATUS_RANK.get(current, 0):
        return current
    return ""


def _mail_order(row: dict) -> tuple:
    return (str(row.get("internal_date") or ""), int(row.get("id") or 0))


def group_key(row: dict) -> str:
    """One Vorgang per application; a mail tied to none is its own."""
    if row.get("bewerbung_id") is not None:
        return f"b{row['bewerbung_id']}"
    return f"e{row.get('id')}"


def firma_of(row: dict) -> str:
    return (row.get("bewerbung_firma") or row.get("job_company")
            or row.get("from_addr") or "—")


def vorgaenge(rows: list[dict]) -> list[dict]:
    """Group waiting mail by the application it belongs to, newest deciding.

    Invitations first — the one thing here with a date and a person waiting
    for an answer — then the freshest conversation. Drawn in arrival order an
    invitation sat between two receipts looking exactly like them."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(group_key(row), []).append(row)
    out = []
    for key, mails in groups.items():
        mails = sorted(mails, key=_mail_order, reverse=True)
        lead = mails[0]
        status = str(lead.get("bewerbung_status") or "")
        out.append({
            "key": key,
            "bewerbung_id": lead.get("bewerbung_id"),
            "firma": firma_of(lead),
            "status": status,
            "closed": is_closed(status),
            "invitation": any((m.get("classification") or "") == "einladung"
                              for m in mails),
            "mails": mails,
            "lead": lead,
            "count": len(mails),
        })
    # Two stable passes rather than one exotic key: newest first, then
    # invitations lifted over them, so the secondary order stays readable.
    out.sort(key=lambda g: _mail_order(g["lead"]), reverse=True)
    out.sort(key=lambda g: not g["invitation"])
    return out


def in_view(group: dict, view_key: str) -> bool:
    if view_key == "offen":
        return not group["closed"]
    if view_key == "abgeschlossen":
        return group["closed"]
    return True


def row_meta(group: dict) -> str:
    """What the row says under the subject, status FIRST.

    Stellen leads with age because its row's job is to make him want to read.
    Here the row's job is to say whether a fast press is safe, and the line is
    nowrap with an ellipsis — so the deciding fact must not be last."""
    lead = group["lead"]
    parts = []
    if group["status"]:
        parts.append(f"Stand: {group['status']}")
    elif group["bewerbung_id"] is None:
        parts.append("Noch keine Bewerbung")
    when = clock(lead.get("internal_date") or lead.get("created_at") or "")
    if when:
        parts.append(when)
    origin = MATCHED_SHORT.get(lead.get("matched_by") or "", "")
    if origin:
        parts.append(origin)
    if group["count"] > 1:
        parts.append(f"{group['count']} Mails")
    return " · ".join(parts)


def verdict_reason(group: dict, classification: str) -> str:
    """The consequence of this button, written under it — never a tooltip.

    He triages from the keyboard, and a consequence only a mouse can reveal is
    a consequence he never sees."""
    wanted = CLASSIFICATION_TO_STATUS.get(classification, "")
    current = group["status"]
    if group["bewerbung_id"] is None:
        return "ordnet die Mail ein — keine Bewerbung verknüpft"
    if not wanted:
        return ""
    if current == wanted:
        return f"ändert nichts — der Stand ist schon {current}"
    kept = kept_status(current, classification)
    if kept:
        return f"Stand bleibt {kept}"
    return f"ändert den Stand: {current} → {wanted}"


def verdict_heading(row: dict) -> str:
    proposal = str(row.get("classification") or "")
    if not proposal:
        return "kein Verdikt — die Regeln konnten diese Mail nicht lesen"
    word = CLASS_LABELS.get(proposal, proposal)
    source = ("KI-Vorschlag" if row.get("classified_by") == "llm"
              else "gelesen als")
    return f"{source} {word}"


def reader_notes(group: dict) -> list[tuple[str, str]]:
    """Worst first, as in Stellen's reader."""
    notes = []
    if group["closed"]:
        notes.append(
            (f"Diese Bewerbung ist bereits abgeschlossen: {group['status']}. "
             f"Ein Urteil hier ordnet die Mail ein und lässt den Stand "
             f"stehen.", "danger"))
    if (group["lead"].get("matched_by") or "") == "name":
        notes.append(
            ("Über den Firmennamen zugeordnet — das ist eine Ähnlichkeit, "
             "keine Identifikation. Prüf, ob die Mail wirklich zu dieser "
             "Bewerbung gehört.", "warn"))
    return notes


def is_receipt_proposal(row: dict) -> bool:
    return (row.get("matched_by") == reply_service.MATCHED_RECEIPT
            and row.get("job_id") is not None
            and row.get("bewerbung_id") is None)


def _when(row: dict) -> str:
    return clock(row.get("internal_date") or row.get("created_at") or "")


def _sender_line(row: dict) -> str:
    origin = MATCHED_BY.get(row.get("matched_by") or "", "")
    parts = [part for part in (firma_of(row), origin, _when(row)) if part]
    return " · ".join(parts)


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
        pending = [dict(row) for row in db.pending_review_replies(con)]
        return {
            "signature": sig,
            "groups": vorgaenge(pending),
            "settled": [dict(row)
                        for row in db.list_inbound_replies(con, LEDGER_LIMIT)],
            "last_poll": db.get_setting(con, reply_service.LAST_POLL_KEY, ""),
            "last_error": db.get_setting(con, reply_service.LAST_ERROR_KEY, ""),
            "ai_on": (
                db.ai_enabled(con)
                and app_settings.boolean(
                    con, reply_service.AI_TOGGLE_KEY, False
                )
            ),
            "connected": gmail.is_connected(),
            "can_read": gmail.can_read(),
            "skipped": db.count_skipped_messages(con),
        }


def _group_fingerprint(group: dict) -> tuple:
    """Everything the LIST draws for this Vorgang. A watcher tick changing none
    of it must not redraw: the signature is global, a 42-item sitting runs half
    an hour, and a rebuild throws away both panes' scroll positions."""
    # FACTS, never the rendered line: `row_meta` runs the date through
    # `clock`, which is relative to now — so a fingerprint built from it
    # changes on its own and the watcher rebuilds the list under him minutes
    # into a sitting. Same class as the unsigned `opened_at` that twice tore
    # down the posting he was reading.
    return (group["key"], group["firma"], group["status"], group["count"],
            group["invitation"],
            str(group["lead"].get("classification") or ""),
            str(group["lead"].get("subject") or ""),
            str(group["lead"].get("matched_by") or ""),
            str(group["lead"].get("internal_date") or ""))


def _mail_fingerprint(row: dict) -> tuple:
    return (int(row.get("id") or 0), str(row.get("classification") or ""),
            int(row.get("needs_review") or 0), row.get("bewerbung_id"),
            str(row.get("matched_by") or ""),
            str(row.get("classified_by") or ""))


@ui.page(ANTWORTEN_PATH)
async def antworten_page():
    async with frame("Antworten", current="antworten", padded=False):
        refresh_gen = {"n": 0}
        state = {"view": DEFAULT_VIEW, "selected": "", "search": "",
                 "prefer_index": None}
        shown: dict[str, dict] = {}
        order: list[str] = []
        drawn: dict = {"list": None, "reader": None, "strip": None}
        row_elements: dict[str, ui.element] = {}
        last_view: dict = {"v": None}
        # Vorgänge he opened this sitting stay marked read, so the gutter is a
        # visible track through the pile. Session-only: no column, no stamp.
        read_here: set[str] = set()

        # Dialogs and post-await messages live in a sibling of the panes,
        # never in a row a rebuild clears.
        overlay = ui.column().classes("contents")

        def say(message: str, **kwargs) -> None:
            with overlay:
                ui.notify(message, **kwargs)

        with ui.column().classes("jd-screen w-full"):
            with ui.row().classes("jd-strip"):
                ui.label("Antworten").classes("jd-strip-title")
                count_label = ui.label().classes("jd-meta")
                state_label = ui.label().classes("jd-meta")
                bulk_host = ui.row().classes("items-center gap-2")
                chip_host = ui.row().classes("items-center ml-auto gap-2")
                with chip_host:
                    ui.label("j k bewegen · ⏎ Vorschlag übernehmen · "
                             "x keiner Bewerbung zuordnen").classes("jd-meta")
            with ui.element("div").classes("jd-panes"):
                with ui.element("div").classes("jd-list"):
                    with ui.row().classes("items-center gap-2 p-2 border-b"):
                        search_box = ui.input(placeholder="suchen …") \
                            .props("dense outlined clearable debounce=350") \
                            .classes("flex-1")
                        search_box.on_value_change(
                            lambda e: set_search(e.value or ""))
                        ui.select(
                            {view.key: view.label for view in VIEWS},
                            value=DEFAULT_VIEW,
                            label="Ansicht",
                            on_change=lambda e: set_view(e.value),
                        ).mark("view-select").props("dense outlined") \
                            .classes("min-w-40")
                    # Sibling of the rows and ABOVE the scroller, so a broken
                    # reader is on screen in every view. `.jd-laeuft:not(:empty)`
                    # means it costs nothing when the reader is healthy.
                    state_host = ui.column().classes("jd-laeuft w-full gap-0")
                    rows_host = ui.column().classes("jd-rows w-full gap-0") \
                        .props('role=listbox aria-label="Antworten"')
                reader = ui.element("div").classes("jd-reader")

        # ------------------------------------------------------------------
        # the reader's own state — drawn only when it is NOT healthy
        # ------------------------------------------------------------------
        def draw_state(view: dict) -> None:
            state_host.clear()
            with state_host:
                if not view["connected"]:
                    _state_row("Gmail ist nicht verbunden — ohne Verbindung "
                               "kann hier nichts ankommen.", settings=True)
                    return
                if not view["can_read"]:
                    _state_row("Die Verbindung kann senden, aber noch nicht "
                               "lesen. Einmal neu verbinden erteilt den "
                               "Lese-Zugriff (gmail.modify).", settings=True)
                    return
                if view["last_error"]:
                    _state_row(f"⚠ Letzter Lauf: {view['last_error']}")
                if not view["ai_on"]:
                    _state_row("Unklare Antworten bleiben unklar: die "
                               "KI-Einordnung ist aus (Einstellungen → AI).",
                               reason=False)
                if view["skipped"]:
                    with ui.column().classes("jd-laeuft-row gap-1"):
                        ui.label(register.plural(
                            view["skipped"],
                            "Nachricht wurde keiner Bewerbung zugeordnet und "
                            "wird nicht erneut gelesen",
                            "Nachrichten wurden keiner Bewerbung zugeordnet "
                            "und werden nicht erneut gelesen")) \
                            .classes("jd-meta")
                        ui.button("Alle Nachrichten neu prüfen",
                                  on_click=lambda _=None, n=view["skipped"]:
                                      rescan_dialog(n)) \
                            .props("flat dense no-caps")

        def _state_row(text: str, settings: bool = False,
                       reason: bool = True) -> None:
            with ui.column().classes("jd-laeuft-row gap-1"):
                ui.label(text).classes("jd-reason" if reason else "jd-meta")
                if settings:
                    ui.button("Zu den Einstellungen",
                              on_click=lambda: ui.navigate.to(
                                  EINSTELLUNGEN_PATH)) \
                        .props("flat dense no-caps")

        def strip_state(view: dict) -> str:
            if not view["connected"]:
                return "Gmail nicht verbunden"
            if not view["can_read"]:
                return "ohne Lese-Zugriff"
            last = clock(view["last_poll"])
            return (f"gelesen zuletzt {last}" if last
                    else "der erste Lauf steht noch aus")

        # ------------------------------------------------------------------
        # re-arming the reader
        # ------------------------------------------------------------------
        def rescan_dialog(skipped: int) -> None:
            with overlay, ui.dialog() as dialog, ui.card():
                ui.label("Alle Nachrichten neu prüfen").classes("font-medium")
                ui.label(register.plural(
                    skipped,
                    "Nachricht wird wieder als ungelesen behandelt und beim "
                    "nächsten Lauf erneut beurteilt.",
                    "Nachrichten werden wieder als ungelesen behandelt und "
                    "beim nächsten Lauf erneut beurteilt.")) \
                    .classes("jd-card-sub")
                ui.label("Bereits zugeordnete Antworten bleiben, wie sie "
                         "sind — sie können nicht doppelt eingetragen "
                         "werden.").classes("jd-card-sub")
                days = ui.number("Wie weit zurück? (Tage)", value=90,
                                 min=1, max=3650, precision=0) \
                    .classes("w-full")
                with ui.row().classes("items-center gap-2"):
                    async def go(_=None):
                        chosen = int(days.value or 90)
                        dialog.close()
                        overlay.clear()
                        result = await run.io_bound(
                            reply_service.rescan, chosen)
                        say(register.plural(
                            result["forgotten"],
                            "Nachricht wird neu gelesen",
                            "Nachrichten werden neu gelesen")
                            + f" · {result['lookback_days']} Tage zurück. "
                            "Der nächste Lauf beginnt damit.")
                        await refresh(force=True)
                    ui.button("Neu prüfen", on_click=go) \
                        .props("unelevated no-caps")
                    ui.button("Abbrechen", on_click=dialog.close) \
                        .props("flat no-caps")
            dialog.open()

        # ------------------------------------------------------------------
        # the list
        # ------------------------------------------------------------------
        def matches(group: dict, needle: str) -> bool:
            if not needle:
                return True
            needle = needle.casefold()
            if needle in group["firma"].casefold():
                return True
            return any(needle in str(m.get("subject") or "").casefold()
                       or needle in str(m.get("from_addr") or "").casefold()
                       for m in group["mails"])

        def visible(view: dict) -> list[dict]:
            if state["view"] == "eingeordnet":
                return []
            return [g for g in view["groups"]
                    if in_view(g, state["view"])
                    and matches(g, state["search"])]

        def render_rows(groups: list[dict]) -> None:
            rows_host.clear()
            row_elements.clear()
            with rows_host:
                if not groups:
                    empty = next(v.empty for v in VIEWS
                                 if v.key == state["view"])
                    ui.label(empty).classes("p-4 jd-meta")
                    return
                for group in groups:
                    _row(group)

        def _row(group: dict) -> None:
            key = group["key"]
            # `div role=option`, never a button: `ui.keyboard`'s `ignore` list
            # contains "button" and the browser focuses a button on mousedown,
            # which killed the keyboard on Stellen outright.
            row = ui.element("div").classes("jd-row").props(
                f'role=option '
                f'data-unread={"false" if key in read_here else "true"} '
                f'aria-selected={"true" if key == state["selected"] else "false"}'
            )
            row.on("click", lambda _=None, k=key: select(k))
            row_elements[key] = row
            with row:
                ui.element("span").classes("jd-gutter")
                with ui.element("div").classes("jd-row-body"):
                    if group["invitation"]:
                        # It is the one thing here with a date and a person
                        # waiting for an answer. Ranked first is not enough —
                        # drawn like everything else it read like everything
                        # else, which is what he reported the first time.
                        ui.label("Einladung — hier wartet jemand auf deine "
                                 "Antwort").classes("jd-urgent-note")
                    with ui.row().classes("items-center gap-2 no-wrap w-full"):
                        ui.label(group["firma"]).classes("jd-firma")
                        proposal = str(group["lead"].get("classification") or "")
                        ui.label(CLASS_LABELS.get(proposal, proposal) or "—") \
                            .classes("jd-score ml-auto"
                                     + (" hi" if proposal == "einladung"
                                        else ""))
                    ui.label(group["lead"].get("subject") or "(kein Betreff)") \
                        .classes("jd-title")
                    ui.label(row_meta(group)).classes("jd-meta")

        # ------------------------------------------------------------------
        # the reader
        # ------------------------------------------------------------------
        def render_reader(view: dict) -> None:
            reader.clear()
            with reader:
                if state["view"] == "eingeordnet":
                    _render_settled(view["settled"])
                    return
                group = shown.get(state["selected"])
                if group is None:
                    with ui.column().classes("p-6 gap-2 max-w-prose"):
                        ui.label("Kein Vorgang ausgewählt.").classes("jd-meta")
                        _automation_note()
                    return
                _render_group(group)

        def _automation_note() -> None:
            """What files itself, said out loud. The tiering decision
            supersedes the earlier blanket rule that "nothing
            changes a status without you", and a screen that writes statuses
            has to name which ones."""
            ui.label("Eindeutige Absagen und Einladungen im Mail-Verlauf "
                     "einer Bewerbung trägt JobDeck selbst ein, ebenso "
                     "Eingangsbestätigungen aus der Domain der Anzeige. Jede "
                     "Zeile unter „Eingeordnet“ sagt, ob sie automatisch kam, "
                     "und ein Klick korrigiert sie. Alles andere wartet hier "
                     "auf dich.").classes("jd-card-sub")

        def _render_group(group: dict) -> None:
            lead = group["lead"]
            with ui.column().classes("w-full gap-0 p-6"):
                ui.label(group["firma"]).classes("text-xl jd-serif")
                ui.label(lead.get("subject") or "(kein Betreff)") \
                    .classes("text-base mt-1")
                ui.label(_sender_line(lead)).classes("jd-meta mt-2")
                # Triage first: the one thing he does WITHOUT reading.
                with ui.row().classes("gap-2 my-4 flex-wrap"):
                    ui.button("✕ keiner Bewerbung zuordnen",
                              on_click=lambda _=None, r=lead["id"]:
                                  dismiss(r)).props("flat dense no-caps")
                if group["invitation"]:
                    ui.label("Hier wartet jemand auf deine Antwort.") \
                        .classes("jd-urgent-note mb-2")
                for text, kind in reader_notes(group):
                    ui.label(text).classes(f"jd-note {kind} mb-2")
                _render_facts(group)
                body = replies.letter_body(
                    lead.get("body_text") or lead.get("snippet") or "")
                ui.label(body[:MAIL_LIMIT] if body else
                         "Diese Mail zitiert nur eine ältere Nachricht — "
                         "kein eigener Text.") \
                    .style("white-space: pre-wrap").classes("text-sm mt-4")
                if len(body) > MAIL_LIMIT:
                    ui.label(
                        f"Der Text ist hier bei {MAIL_LIMIT:,}"
                        .replace(",", ".")
                        + f" von {len(body):,}".replace(",", ".")
                        + " Zeichen abgeschnitten.").classes("jd-note mt-3")
                # AFTER the text, deliberately: he reads the mail first, then
                # sees what the machine made of it, and only then is offered
                # the press that commits him.
                why = " ".join((lead.get("matched_note") or "").split())
                with ui.element("div").classes("jd-why"):
                    ui.label(verdict_heading(lead)).classes("jd-meta")
                    if why:
                        ui.label(why).classes("text-sm mt-1")
                _render_verdicts(group)
                _render_siblings(group)
                # The old page carried this sentence permanently, above the
                # work. Here it sits under it — still on the screen he decides
                # from, because "what gets written without me" is not a thing
                # the buttons' own consequences can say.
                with ui.column().classes("mt-8 max-w-prose"):
                    _automation_note()

        def _render_facts(group: dict) -> None:
            lead = group["lead"]
            facts = [("Bewerbung", group["firma"])]
            if group["status"]:
                facts.append(("Stand", group["status"]))
            elif group["bewerbung_id"] is None:
                facts.append(("Stand", "noch keine Bewerbung im Register"))
            if lead.get("from_addr"):
                facts.append(("Absender", str(lead["from_addr"])))
            origin = MATCHED_BY.get(lead.get("matched_by") or "", "")
            if origin:
                facts.append(("Erkannt", origin))
            when = _when(lead)
            if when:
                facts.append(("Gelesen", when))
            with ui.element("div").classes("jd-facts mt-4"):
                for key, value in facts:
                    ui.label(key).classes("k")
                    ui.label(value)

        def _render_verdicts(group: dict) -> None:
            lead = group["lead"]
            if is_receipt_proposal(lead):
                with ui.column().classes("gap-1 items-start mt-6"):
                    ui.button("Als Bewerbung eintragen",
                              on_click=lambda _=None, r=lead["id"]: adopt(r)) \
                        .props("unelevated no-caps")
                    ui.label("trägt die Bewerbung ins Register ein") \
                        .classes("jd-reason")
                return
            proposal = str(lead.get("classification") or "")
            with ui.row().classes("gap-3 flex-wrap mt-6 items-start"):
                for value in VERDICTS:
                    with ui.column().classes("gap-1 items-start max-w-64"):
                        ui.button(
                            CLASS_LABELS[value],
                            on_click=lambda _=None, r=lead["id"], v=value:
                                classify(r, v)) \
                            .mark(f"verdict-{value}") \
                            .props(("unelevated" if value == proposal
                                    else "outline") + " no-caps")
                        reason = verdict_reason(group, value)
                        if reason:
                            ui.label(reason).classes("jd-reason")
                        # The second, explicit press — never a dialog: an
                        # override always one press away is a speed bump, and
                        # he rejected that shape for the letter cap.
                        if kept_status(group["status"], value):
                            ui.button(
                                "Stand trotzdem ändern",
                                on_click=lambda _=None, r=lead["id"], v=value:
                                    classify(r, v, force_status=True)) \
                                .mark(f"force-{value}") \
                                .props("flat dense no-caps")

        def _render_siblings(group: dict) -> None:
            if group["count"] < 2:
                return
            with ui.column().classes("mt-6 gap-1 w-full"):
                ui.label("Weitere Mails in diesem Verlauf").classes("jd-meta")
                for mail in group["mails"][1:]:
                    with ui.element("div").classes("jd-siblings"):
                        with ui.row().classes("items-center gap-2 no-wrap"):
                            ui.label(_when(mail)).classes("jd-meta")
                            ui.label(mail.get("subject") or "(kein Betreff)") \
                                .classes("text-sm") \
                                .style("overflow:hidden;text-overflow:"
                                       "ellipsis;white-space:nowrap")
                            ui.space()
                            ui.button("Ganze Mail",
                                      on_click=lambda _=None, m=dict(mail):
                                          show_mail(m)) \
                                .props("flat dense no-caps")

        # ------------------------------------------------------------------
        # Eingeordnet — the same panes, a different source
        # ------------------------------------------------------------------
        def _render_settled(settled: list[dict]) -> None:
            with ui.column().classes("w-full gap-0 p-6"):
                ui.label("Eingeordnet").classes("text-xl jd-serif")
                if not settled:
                    ui.label("Noch keine Antwort gelesen.") \
                        .classes("jd-meta mt-2")
                    return
                line = register.plural(len(settled), "Antwort ist eingeordnet",
                                       "Antworten sind eingeordnet")
                if len(settled) >= LEDGER_LIMIT:
                    # It shows the newest N. Saying so is the difference
                    # between a ledger and a ledger that quietly ends.
                    line += f" — die neuesten {LEDGER_LIMIT}"
                ui.label(line).classes("jd-meta mt-2")
                with ui.column().classes("mt-3 max-w-prose"):
                    _automation_note()
                for row in settled:
                    _settled_row(row)

        def _settled_row(row: dict) -> None:
            classification = str(row.get("classification") or "")
            dismissed = not classification and row.get("bewerbung_id") is None
            with ui.row().classes("w-full items-center gap-2 no-wrap mt-3"):
                ui.label(_when(row)).classes("jd-meta")
                ui.label(firma_of(row)).classes("font-medium") \
                    .style("overflow:hidden;text-overflow:ellipsis;"
                           "white-space:nowrap")
                with ui.element("span").classes(
                        "jd-pill" if dismissed else
                        f"jd-pill {_PILL.get(classification, '')}"):
                    ui.label("Abgelegt" if dismissed else
                             CLASS_LABELS.get(classification, classification))
                ui.label("" if dismissed else
                         "bestätigt" if row.get("classified_by")
                         == "reply_manual" else "automatisch") \
                    .classes("jd-meta")
                ui.space()
                if dismissed:
                    # `dismiss_review` keeps the row, so putting it back on the
                    # shelf is one flag — without a button the press was a
                    # one-way door with no schema reason to be one.
                    ui.button("Zurück zur Prüfung",
                              on_click=lambda _=None, r=row["id"]:
                                  reopen(r)).props("flat dense no-caps")
                elif (row.get("matched_by") == reply_service.MATCHED_RECEIPT
                        and row.get("bewerbung_id") is not None):
                    ui.button("Rückgängig",
                              on_click=lambda _=None, r=row["id"]:
                                  undo(r)).props("flat dense no-caps")
                elif row.get("bewerbung_id") is not None:
                    ui.button("Korrigieren",
                              on_click=lambda _=None, r=dict(row):
                                  correct(r)).props("flat dense no-caps")
                ui.button("Ganze Mail",
                          on_click=lambda _=None, r=dict(row): show_mail(r)) \
                    .props("flat dense no-caps")

        def correct(row: dict) -> None:
            current = str(row.get("classification") or "")
            with overlay, ui.dialog() as dialog, ui.card():
                ui.label("Wie war diese Antwort gemeint?") \
                    .classes("font-medium")
                ui.label(_sender_line(row)).classes("jd-card-sub")
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    for value in VERDICTS:
                        async def pick(_=None, v=value):
                            dialog.close()
                            # The deliberate path: he opened a dialog on a row
                            # that is already filed and chose. The shelf's
                            # "never go backwards on one press" guard is about
                            # a FIRST press on a proposal; here it would turn
                            # "ein Klick korrigiert sie" into two.
                            await classify(row["id"], v, force_status=True)
                        ui.button(CLASS_LABELS[value], on_click=pick) \
                            .mark(f"correct-{value}") \
                            .props(("unelevated" if value == current
                                    else "outline") + " no-caps")
                ui.button("Abbrechen", on_click=dialog.close) \
                    .props("flat no-caps")
            dialog.open()

        def show_mail(row: dict) -> None:
            with overlay, ui.dialog() as dialog, ui.card().classes(
                    "w-[44rem] max-w-full"):
                ui.label(row.get("subject") or "(kein Betreff)") \
                    .classes("font-medium")
                ui.label(_sender_line(row)).classes("jd-card-sub")
                body = replies.letter_body(
                    row.get("body_text") or row.get("snippet") or "")
                ui.label(body[:MAIL_LIMIT] or "(kein Text gespeichert)") \
                    .style("white-space: pre-wrap").classes("text-sm")
                ui.button("Schließen", on_click=dialog.close) \
                    .props("flat no-caps")
            dialog.open()

        # ------------------------------------------------------------------
        # actions
        # ------------------------------------------------------------------
        async def classify(row_id: int, classification: str,
                           force_status: bool = False) -> None:
            _hold_place()
            outcome = await run.io_bound(
                reply_service.resolve_review, row_id, classification,
                force_status)
            word = CLASS_LABELS.get(classification, classification)
            if not outcome["ok"]:
                say("Diese Zeile gibt es nicht mehr.", type="warning")
            elif outcome["status_written"]:
                say(f"Eingeordnet als {word} — der Stand der Bewerbung ist "
                    f"nachgezogen.", type="positive")
            elif outcome["kept"]:
                say(f"Eingeordnet als {word} — der Stand bleibt "
                    f"{outcome['kept']}.", type="positive")
            else:
                say(f"Eingeordnet als {word}.", type="positive")
            await refresh(force=True)

        async def adopt(row_id: int) -> None:
            _hold_place()
            outcome = await run.io_bound(reply_service.adopt_receipt, row_id)
            if outcome.get("ok"):
                say("Bewerbung eingetragen — sie steht jetzt im Register.",
                    type="positive")
            elif outcome.get("duplicate") is not None:
                say(hold_line(outcome.get("decision"),
                              outcome.get("company", "")), type="warning")
            else:
                say("Eintragen ging nicht — die Anzeige fehlt.",
                    type="warning")
            await refresh(force=True)

        async def dismiss(row_id: int) -> None:
            _hold_place()
            await run.io_bound(reply_service.dismiss_review, row_id)
            say("Abgelegt — keiner Bewerbung zugeordnet.")
            await refresh(force=True)

        async def reopen(row_id: int) -> None:
            await run.io_bound(reply_service.reopen_review, row_id)
            say("Wieder auf dem Prüfstapel.", type="positive")
            await refresh(force=True)

        async def undo(row_id: int) -> None:
            ok = await run.io_bound(reply_service.undo_receipt, row_id)
            say("Bewerbung wieder ausgetragen — die Mail wartet unter "
                "„Zu prüfen“." if ok
                else "Da war nichts mehr rückgängig zu machen.",
                type="positive" if ok else "warning")
            await refresh(force=True)

        async def file_closed(ids: list[int]) -> None:
            """One gesture for the June archive: file every waiting mail of
            every closed application, WITHOUT writing a single status.

            Deliberately not a bulk verdict. `resolve_review` writes as
            'reply_manual', which the anti-downgrade rank exempts, so a bulk
            confirm would be a dozen unguarded status writes in one press."""
            overlay.clear()
            filed = await run.io_bound(reply_service.dismiss_many, ids)
            say(register.plural(filed, "Mail abgelegt", "Mails abgelegt")
                + " — kein Stand wurde geändert.", type="positive")
            await refresh(force=True)

        def confirm_file_closed(groups: list[dict]) -> None:
            ids = [int(m["id"]) for g in groups for m in g["mails"]]
            with overlay, ui.dialog() as dialog, ui.card():
                ui.label("Alle ablegen").classes("font-medium")
                ui.label(register.plural(
                    len(ids),
                    "Mail zu einer bereits abgeschlossenen Bewerbung wird "
                    "abgelegt.",
                    "Mails zu bereits abgeschlossenen Bewerbungen werden "
                    "abgelegt.")).classes("jd-card-sub")
                ui.label("Kein Stand wird geändert, nichts wird gelöscht — "
                         "die Mails stehen danach unter „Eingeordnet“ und "
                         "können einzeln zurückgeholt werden.") \
                    .classes("jd-card-sub")
                with ui.row().classes("items-center gap-2"):
                    async def go(_=None):
                        dialog.close()
                        await file_closed(ids)
                    ui.button("Ablegen", on_click=go) \
                        .props("unelevated no-caps")
                    ui.button("Abbrechen", on_click=dialog.close) \
                        .props("flat no-caps")
            dialog.open()

        # ------------------------------------------------------------------
        # selection and keyboard
        # ------------------------------------------------------------------
        def select(key: str) -> None:
            if key == state["selected"] or key not in shown:
                return
            previous = state["selected"]
            state["selected"] = key
            read_here.add(key)
            # Rewriting props rather than rebuilding: a rebuild on every
            # keystroke throws away both panes' scroll positions.
            for row_key in (previous, key):
                element = row_elements.get(row_key)
                if element is not None:
                    element.props(
                        f'aria-selected={"true" if row_key == key else "false"} '
                        f'data-unread='
                        f'{"false" if row_key in read_here else "true"}')
            view = last_view["v"]
            if view is not None:
                render_reader(view)
                drawn["reader"] = _reader_state(view)

        def move(step: int) -> None:
            if not order:
                return
            if state["selected"] not in order:
                select(order[0])
                return
            index = order.index(state["selected"])
            select(order[max(0, min(len(order) - 1, index + step))])

        acting = {"busy": False}

        async def _once(make) -> None:
            """One keyboard action at a time.

            A verdict is a write in a worker thread; a second keystroke landing
            while it is in flight reads the SAME selection — the row has not
            left the list yet — and files the same mail twice while the row
            below it is silently skipped. j and k stay free: moving is cheap
            and reversible."""
            # A THUNK, not a coroutine: building the coroutine at the call
            # site and then refusing it here leaves it never awaited.
            if acting["busy"]:
                return
            acting["busy"] = True
            try:
                await make()
            finally:
                acting["busy"] = False

        def _hold_place() -> None:
            """Remember where in the list he acted, so the Vorgang that takes
            that place is the one he lands on."""
            state["prefer_index"] = (order.index(state["selected"])
                                     if state["selected"] in order else None)

        async def on_key(event) -> None:
            # Held keys repeat ~30 times a second: without this, holding ⏎
            # would file a dozen verdicts on a dozen applications.
            if not event.action.keydown or event.action.repeat:
                return
            if event.modifiers.ctrl or event.modifiers.meta \
                    or event.modifiers.alt:
                return
            # A dialog sits OVER the list; reaching for its close button and
            # hitting 'x' must not file the Vorgang underneath it.
            if live.dialog_open():
                return
            key = str(event.key)
            if key in ("j", "ArrowDown"):
                move(1)
                return
            if key in ("k", "ArrowUp"):
                move(-1)
                return
            group = shown.get(state["selected"])
            if group is None:
                return
            lead = group["lead"]
            if key == "x":
                await _once(lambda: dismiss(lead["id"]))
            elif key == "Enter":
                # Only the machine's own proposal, and only where it may be
                # written on one press: a keystroke must never be the thing
                # that changes a status the buttons would have guarded.
                proposal = str(lead.get("classification") or "")
                if (proposal in CLASSIFICATION_TO_STATUS
                        and not is_receipt_proposal(lead)):
                    await _once(lambda: classify(lead["id"], proposal))

        # ------------------------------------------------------------------
        # loading and drawing
        # ------------------------------------------------------------------
        def _reader_state(view: dict) -> tuple:
            group = shown.get(state["selected"])
            return (state["view"], state["selected"],
                    tuple(_mail_fingerprint(m) for m in group["mails"])
                    if group else (),
                    group["status"] if group else "",
                    tuple(_mail_fingerprint(r) for r in view["settled"])
                    if state["view"] == "eingeordnet" else ())

        def draw_strip(view: dict, groups: list[dict]) -> None:
            closed = [g for g in view["groups"] if g["closed"]]
            if state["view"] == "eingeordnet":
                count_label.set_text(register.plural(
                    len(view["settled"]), "Antwort eingeordnet",
                    "Antworten eingeordnet"))
            else:
                # The rail counts MAILS ("42 zu prüfen") and this counts
                # Vorgänge, so without the second figure the two screens
                # disagree out loud and neither says why.
                mails = sum(g["count"] for g in view["groups"])
                line = register.plural(len(groups), "Vorgang wartet",
                                       "Vorgänge warten")
                if mails != len(groups):
                    line += " · " + register.plural(
                        mails, "Mail insgesamt", "Mails insgesamt")
                count_label.set_text(line)
            state_label.set_text(strip_state(view))
            bulk_host.clear()
            with bulk_host:
                if state["view"] == "abgeschlossen" and groups:
                    # `groups`, not every closed Vorgang: the button files what
                    # the list SHOWS. Keyed on the unfiltered set it filed a
                    # search's worth of mail he could not see, and "Alle" would
                    # have meant something different from everything on screen.
                    ui.button("Alle ablegen",
                              on_click=lambda _=None, g=list(groups):
                                  confirm_file_closed(g)) \
                        .props("flat dense no-caps")
                elif state["view"] == "offen" and closed:
                    ui.label(register.plural(
                        len(closed),
                        "Vorgang zu einer abgeschlossenen Bewerbung "
                        "ausgeblendet",
                        "Vorgänge zu abgeschlossenen Bewerbungen "
                        "ausgeblendet")).classes("jd-meta")

        async def refresh(force: bool = False) -> None:
            # Last request wins: a click's refresh and the watcher's tick can
            # be in flight together, and the older result finishing second
            # would repaint the page with rows from before the write.
            refresh_gen["n"] += 1
            generation = refresh_gen["n"]
            view = await run.io_bound(_load)
            if view is None or generation != refresh_gen["n"]:
                return
            live_view.mark(view["signature"])
            last_view["v"] = view
            groups = visible(view)
            shown.clear()
            shown.update({g["key"]: g for g in groups})
            order.clear()
            order.extend(g["key"] for g in groups)
            if state["selected"] not in shown:
                # He acted on it, so it is MEANT to be gone: the Vorgang that
                # took its place is next. Landing back at the top made a
                # 42-item sitting cost O(n²) keystrokes — the very thing this
                # screen exists to stop, and the same defect Stellen's
                # `prefer_index` was added for.
                index = state["prefer_index"]
                if index is not None and order:
                    state["selected"] = order[min(index, len(order) - 1)]
                else:
                    state["selected"] = order[0] if order else ""
                if state["selected"]:
                    read_here.add(state["selected"])
            # Belongs to HIS action, so HIS redraw ends it — whether or not the
            # Vorgang survived. Cleared on every refresh it is lost to the
            # watcher tick that lands mid-write, and the cursor jumps to the
            # top; never cleared it outlives its action, because a Vorgang
            # holding several mails survives a verdict on one of them, and it
            # then steers some later, unrelated redraw onto an index that means
            # nothing any more.
            if force:
                state["prefer_index"] = None

            # NOT keyed on the selection: moving the cursor rewrites two rows'
            # props in place, and putting `selected` here made the watcher's
            # next tick rebuild the whole list under him — throwing away the
            # scroll position on every j.
            list_state = (state["view"], state["search"],
                          tuple(_group_fingerprint(g) for g in groups))
            strip = (len(groups), strip_state(view), view["skipped"],
                     view["last_error"], view["ai_on"], state["view"],
                     len(view["settled"]),
                     sum(1 for g in view["groups"] if g["closed"]))
            reader_state = _reader_state(view)
            # Three comparisons, not one: the signature is global, so a score
            # landing on a posting he is not even looking at fires this — and
            # a 42-item sitting runs twenty to forty minutes.
            if force or drawn["list"] != list_state:
                render_rows(groups)
                drawn["list"] = list_state
            if force or drawn["strip"] != strip:
                draw_state(view)
                draw_strip(view, groups)
                drawn["strip"] = strip
            if force or drawn["reader"] != reader_state:
                render_reader(view)
                drawn["reader"] = reader_state

        async def set_view(key: str) -> None:
            if key not in _VIEW_KEYS:
                return
            state["view"] = key
            state["selected"] = ""
            read_here.clear()
            await refresh(force=True)

        async def set_search(needle: str) -> None:
            state["search"] = needle.strip()
            await refresh(force=True)

        with chip_host:
            live_view = live.watch(signature, refresh, busy=live.dialog_open)
        # `ignore` is NiceGUI's own client-side rule and the only one that can
        # work: a keystroke typed into the search box must never reach here,
        # and only the browser knows where the caret is.
        ui.keyboard(on_key=on_key,
                    ignore=["input", "select", "button", "textarea"])
        await refresh(force=True)
