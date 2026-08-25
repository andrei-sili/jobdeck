"""Bewerbungen: where the work went, and what came back.

The register was a ported spreadsheet — an English table of every column the
old tracker had, sorted by whichever heading was clicked last. It answered
"what did I write down" and nothing else. The questions he actually has are
about shape: how much of the pile ever becomes an application, whether he is
still sending, and who has gone quiet.

So the screen states five things and lists the rows underneath them. Every
figure is computed in `services/register.py` and only drawn here, which is
what makes the claims testable without a browser.
"""

import datetime

from fastapi.responses import RedirectResponse
from nicegui import app, run, ui

from jobdeck import db, export
from jobdeck.constants import (
    BEANTWORTET_STATUS,
    DRAFT_DELIVERED,
    KANAL_OPTIONS,
    OFFENE_STATUS,
    STATUS_OPTIONS,
)
from jobdeck.dedupe import find_duplicate_bewerbung, fold
from jobdeck.services import register
from jobdeck.ui import live
from jobdeck.ui.helpers import open_in_system
from jobdeck.ui.layout import BEWERBUNGEN_TABS, frame, tabs

BEWERBUNGEN_PATH = "/bewerbungen"

# What the search box looks through. The note is deliberately included: it is
# where the posting's URL and the channel it went out by end up.
SEARCH_FIELDS = ("firma", "email", "ansprechpartner", "plz_ort", "notiz")

VIEW_ALL = "Alle"

# How many silent companies the panel names before it stops and points at
# the register below. Enough to see the shape of the tail, few enough that
# the panel does not become the page.
SILENCE_ROWS = 8

# The height an empty column of the rhythm strip is drawn at, in per cent.
# A day that held something is drawn above it, so the two never read alike.
RHYTHM_FLOOR = 3

# How the register's own status vocabulary is coloured. Amber is for an
# application that is merely waiting, not for a bad outcome — a rejection is a
# closed question and reads calmer than an open one on this screen.
_PILL = {
    "Einladung": "ok",
    "Antwort erhalten": "ok",
    "Absage": "",
    # Nobody refused this one and nobody answered it either. It is a closed
    # question, so it reads as calm as a rejection — never as a bad outcome,
    # and never as something still waiting for him.
    "Keine Antwort": "",
    "Zurückgezogen": "",
}


def _pill_class(status: str) -> str:
    return _PILL.get(status, "warn" if status in OFFENE_STATUS else "")


def _channel_verdict(shares: list[register.Share], rate: bool) -> str:
    """What the two numbers may be read as — and what they may not.

    The mockup asserted that forms answer more often. On the real
    register they answer at 27 % against 26 %, which is one
    application's worth of difference. A screen that leads with the
    higher figure teaches him to steer by noise.
    """
    if not shares:
        # A sentence about "the figures above" printed where no figure
        # is drawn is worse than silence.
        return ""
    if not rate:
        smallest = min(share.whole for share in shares)
        # "ist … noch Rechnen" is not a German construction, "Befund" is
        # medical and "Stückzahlen" belongs to manufacturing.
        return ("Bei "
                + ("nur einer Bewerbung" if smallest == 1
                   else f"nur {smallest} Bewerbungen")
                + " auf einem der Wege sagt eine Prozentzahl noch nichts — "
                  "deshalb stehen hier nur die Zahlen.")
    ranked = sorted(shares, key=lambda s: -s.ratio)
    if len(ranked) < 2:
        return ""
    gap = ranked[0].ratio - ranked[1].ratio
    if gap < 0.10:
        return (f"{ranked[0].label} und {ranked[1].label} liegen "
                "gleichauf — der Unterschied ist kleiner als eine "
                "einzelne Antwort. Danach solltest du dich nicht "
                "richten.")
    return (f"{ranked[0].label} antwortet häufiger — "
            f"{round(ranked[0].ratio * 100)} % gegen "
            f"{round(ranked[1].ratio * 100)} %.")


def _load() -> dict:
    return register.facts()


def _signature() -> tuple:
    with db.db() as con:
        return register.signature(con)


def _one(row_id: int) -> dict | None:
    """Everything the dialog shows, in ONE read: the row, its letter, its
    history. None when the application is gone.

    Read on the server by id, which is the property this whole screen exists
    to keep: the old one handed Quasar's `rowClick` payload straight into the
    dialog, so the path it later gave `xdg-open` and the id it later deleted
    both came from the client. Every row here closes over its id in Python.

    One connection rather than three, and one snapshot: opening a row while
    the poller commits used to be able to show a letter belonging to a state
    the row above it no longer had.
    """
    places = ",".join("?" * len(DRAFT_DELIVERED))
    with db.db() as con:
        found = db.get_bewerbung(con, row_id)
        if found is None:
            return None
        letter = con.execute(
            "SELECT betreff, anschreiben_body, status, pdf_path FROM drafts "
            f" WHERE bewerbung_id=? AND status IN ({places})"
            "  ORDER BY id DESC LIMIT 1", (row_id, *DRAFT_DELIVERED)
        ).fetchone()
        return {
            "row": dict(found),
            "letter": dict(letter) if letter is not None else None,
            "history": [dict(r) for r in db.list_status_history(con, row_id)],
        }


def _save(row_id, values, new_status):
    """Write an application, refusing a date no panel above could read.

    `gesendet_am` was free text. A row saved as "15.08.2026" printed that in
    the register and was read as no date at all by everything else — the age
    column said "?", the silence panel filed it under "seit unbekannt", and the
    rhythm strip moved no column for it. A screen must judge a value the way
    its CONSUMER will, and every consumer here is `date.fromisoformat`.
    """
    stamped = (values.get("gesendet_am") or "").strip()
    if stamped:
        try:
            datetime.date.fromisoformat(stamped)
        except ValueError:
            return ("„Gesendet am“ braucht das Format JJJJ-MM-TT — sonst "
                    "zählt die Bewerbung in keiner Auswertung mit.")
    with db.db() as con:
        dup = find_duplicate_bewerbung(
            con, values["firma"], values["email"], exclude_id=row_id
        )
        if dup is not None:
            return (f"Bei {dup['firma']} steht schon eine Bewerbung vom "
                    f"{dup['gesendet_am']} — eine Bewerbung pro Firma.")
        if row_id is None:
            db.add_bewerbung(con, {**values, "status": new_status})
        else:
            db.update_bewerbung(con, row_id, values)
            db.set_status(con, row_id, new_status, source="user")
    return None


def _delete(row_id):
    with db.db() as con:
        db.delete_bewerbung(con, row_id)


def _letter(row_id):
    """The Anschreiben that went out with this application, if the app wrote it.

    Only a letter this application actually carried: 'sent' means an e-mail
    from here, 'filed' means it went inside the Mappe he uploaded. A draft that
    merely happens to sit on the same posting is not what the employer read,
    so the status is part of the query and not merely of the docstring.
    """
    found = _one(row_id)
    return found["letter"] if found else None


@app.get("/applications")
def legacy_applications_page():
    """The register's old address. Kept as a redirect for the same reason
    `/jobs` and `/profiles` are: a bookmark that 404s reads as a lost feature."""
    return RedirectResponse(BEWERBUNGEN_PATH)


@ui.page(BEWERBUNGEN_PATH)
async def bewerbungen_page():
    async with frame("Bewerbungen", current="bewerbungen"):
        state = {"query": "", "status": VIEW_ALL}
        drawn: dict = {}
        refresh_gen = {"n": 0}

        tabs("register", BEWERBUNGEN_TABS)
        header = ui.row().classes("w-full items-center gap-3")
        container = ui.column().classes("w-full gap-4")
        # A permanent sibling of the panels rather than a child of them, so
        # typing in the search box redraws the list alone: rebuilding the five
        # panels on every keystroke moves the whole screen under the cursor.
        register_host = ui.column().classes("w-full")
        # Dialogs and post-await messages live in a sibling of the container,
        # never in the row that opened them: a handler runs in the slot of its
        # own button, refresh() clears `container`, and this page refreshes on
        # a timer — so a coroutine parked on `await confirm` would never
        # resolve and a notification would be raised into a deleted slot.
        overlay = ui.column().classes("contents")

        def say(message: str, **kwargs) -> None:
            with overlay:
                ui.notify(message, **kwargs)

        # ------------------------------------------------------------------
        # 1. The numbers: the register, and what came back
        # ------------------------------------------------------------------
        def draw_numbers(view: dict) -> None:
            """Two groups that each add up, in place of the funnel.

            The funnel counted postings — found, scored, opened, written to —
            and it belonged to a screen about applications only as long as
            nowhere else said those things. Stellen says all of them now, on
            the rows themselves, so keeping the column here was asking him to
            read the pipeline twice and reconcile it. He put it plainly: the
            statistic is unintelligible and should be simple.
            """
            with ui.column().classes("jd-card gap-3"):
                ui.label("Das Register").classes("jd-card-title")
                ui.label("Alles, was du je abgeschickt hast — auch vor "
                         "JobDeck.").classes("jd-card-sub")
                with ui.element("div").classes("jd-funnel"):
                    for step in register.ledger(view):
                        _funnel_row(step, dim=step.key != "register")
                ui.element("div").classes("h-1")
                ui.label("Was zurückkam").classes("jd-card-title")
                ui.label("Die beantworteten Bewerbungen, aufgeteilt danach, "
                         "was geantwortet wurde.").classes("jd-card-sub")
                with ui.element("div").classes("jd-funnel"):
                    for step in register.answers(view["apps"]):
                        # All three dim, because all three are PARTS. A solid
                        # bar means "this is the figure the others are shares
                        # of", and the whole they are shares of is the
                        # "beantwortet" line in the group above. Highlighting
                        # the invitation instead put the one solid bar on the
                        # only value that is nought — a bright bar nobody can
                        # see, over two real numbers drawn as though they were
                        # the aside.
                        _funnel_row(step, dim=True)
                answered = {s.key: s for s in register.ledger(view)}
                sentence, over = register.answer_time(
                    view["answer_delays"], answered["beantwortet"].count)
                if sentence:
                    ui.label(sentence).classes("jd-note mt-2")
                    ui.label(over).classes("jd-meta")

        def _funnel_row(step: register.Step, dim: bool = False) -> None:
            ui.label(step.label).classes("name")
            with ui.element("span").classes("jd-bar"):
                ui.element("i").classes("dim" if dim else "") \
                    .style(f"width:{round(register.clamp(step.share) * 100, 1)}%")
            ui.label(f"{step.count:,}".replace(",", ".")).classes("num jd-mono")
            if step.note:
                ui.label(step.note).classes("why")

        # ------------------------------------------------------------------
        # 2. The rhythm: sixty days, and the pauses in them
        # ------------------------------------------------------------------
        def draw_rhythm(view: dict, today: datetime.date) -> None:
            strip = register.rhythm(view["apps"], today)
            peak = register.busiest(strip)
            pause = register.longest_pause(strip)
            with ui.column().classes("jd-card gap-3"):
                ui.label("Rhythmus").classes("jd-card-title")
                ui.label(f"Die letzten {register.RHYTHM_DAYS} Tage, ein Tag "
                         "je Säule.").classes("jd-card-sub")
                with ui.element("div").classes("jd-rhythm"):
                    for day in strip:
                        share = day.count / peak.count if peak else 0.0
                        # An empty day is drawn at the floor; a day that HELD
                        # something must clear it, or one application beside a
                        # forty-application day is the same grey stub as a day
                        # nothing went out on.
                        height = (max(RHYTHM_FLOOR + 4, round(share * 100))
                                  if day.count else RHYTHM_FLOOR)
                        ui.element("i").classes(
                            "empty" if not day.count else
                            "today" if day.date == today else ""
                        ).style(f"height:{height}%")
                with ui.element("div").classes("jd-ends"):
                    ui.label(register.de_day(strip[0].date))
                    # "ohne Pause" is only true of a window that HAS working
                    # days to be uninterrupted between. Printed over sixty
                    # empty columns it praised a stretch in which nothing at
                    # all went out.
                    ui.label(register.plural(pause, "Tag Pause", "Tage Pause")
                             or ("ohne Pause" if peak else "keine Bewerbung"))
                    ui.label("heute")
                if peak is None:
                    ui.label("In diesem Zeitraum ist nichts rausgegangen.") \
                        .classes("jd-card-sub")
                else:
                    # Stated as an event, the way the approved mockup did it:
                    # "der vollste Tag" is a superlative of an absolute
                    # adjective, and it printed a date when that day is today.
                    when = ("Heute" if peak.date == today
                            else f"Am {register.de_day(peak.date)}")
                    ui.label(
                        f"{when} gingen "
                        + register.plural(peak.count, "Bewerbung", "Bewerbungen")
                        + " raus"
                        + (", die längste Pause davor oder danach dauerte "
                           + register.plural(pause, "Tag", "Tage")
                           if pause else "")
                        + ".").classes("jd-card-sub")

        # ------------------------------------------------------------------
        # 3. Who is silent, and since when
        # ------------------------------------------------------------------
        def draw_silence(view: dict, today: datetime.date) -> None:
            waiting = register.silence(view["apps"], view["follow_up_days"],
                                       today)
            overdue = [row for row in waiting if row.overdue]
            with ui.column().classes("jd-card gap-3"):
                ui.label("Wer schweigt, seit wann").classes("jd-card-title")
                # "die längste zuerst" agrees with die Bewerbung, so it
                # promised the longest LETTER first; and "wird die Zahl
                # bernstein" is not German — a screen should not describe its
                # own CSS colour either.
                ui.label(
                    register.plural(len(waiting), "Bewerbung ohne Antwort",
                                    "Bewerbungen ohne Antwort")
                    + " — die am längsten wartende zuerst. Ab "
                    + f"{view['follow_up_days']} Tagen ist sie überfällig."
                ).classes("jd-card-sub")
                if not waiting:
                    # "Jede Bewerbung ist beantwortet" is also what an EMPTY
                    # register produces, and what one holding only withdrawn
                    # applications produces — neither is an achievement.
                    ui.label("Noch keine Bewerbung im Register."
                             if not view["apps"]
                             else "Keine Bewerbung wartet gerade auf eine "
                                  "Antwort.").classes("jd-card-sub")
                    return
                longest = max((row.days or 0) for row in waiting) or 1
                shown = waiting[:SILENCE_ROWS]
                with ui.element("div").classes("jd-wait"):
                    for index, row in enumerate(shown):
                        last = " last" if index == len(shown) - 1 else ""
                        ui.label(row.firma).classes("firma" + last)
                        with ui.element("span").classes("jd-bar" + last) \
                                .style("align-self:center"):
                            # Through the shared clamp: a date in the FUTURE
                            # gives a negative age, and `width:-98%` is invalid
                            # CSS that the browser drops — leaving a block
                            # element at `width:auto`, so the row furthest from
                            # overdue drew the longest bar on the panel.
                            width = register.clamp((row.days or 0) / longest)
                            ui.element("i").classes("warn" if row.overdue else "") \
                                .style(f"width:{round(width * 100)}%")
                        ui.label(
                            f"{row.days} T" if row.days is not None
                            # An imported row can state no date at all. "0 T"
                            # would put it at the head of a list whose whole
                            # order is a claim about age.
                            else "ohne Datum"
                        ).classes("age" + (" over" if row.overdue else "") + last)
                if len(waiting) > len(shown):
                    ui.label(
                        "… und "
                        + register.plural(len(waiting) - len(shown),
                                          "weitere", "weitere")
                        + ". Die vollständige Liste steht unten."
                    ).classes("jd-card-sub")
                if overdue:
                    ui.label(
                        register.plural(len(overdue), "davon wartet",
                                        "davon warten")
                        + f" länger als {view['follow_up_days']} Tage. "
                          "Nachfassen geht noch von Hand — JobDeck schreibt "
                          "die Nachfass-E-Mail noch nicht."
                    ).classes("jd-card-sub")

        # ------------------------------------------------------------------
        # 4 + 5. What answers, and what each board brings
        # ------------------------------------------------------------------
        def draw_shares(view: dict) -> None:
            channels = register.by_channel(view["apps"])
            sources = register.by_source(view["sources"])
            with ui.row().classes("w-full gap-4 items-stretch no-wrap"):
                with ui.column().classes("jd-card gap-3"):
                    ui.label("Was antwortet").classes("jd-card-title")
                    ui.label("Beantwortete Bewerbungen je Weg.") \
                        .classes("jd-card-sub")
                    # The bar is the RATE when the rate may be stated, and
                    # the POPULATION when it may not. Drawn as the population
                    # it says the opposite of the sentence beneath it (41
                    # against 35 out-draws two equal rates); drawn as the rate
                    # while the card is refusing to print percentages, it
                    # publishes exactly the percentages it just called noise —
                    # one application on a third channel drawing a full bar.
                    rate = register.enough_for_a_rate(channels)
                    _share_rows(channels, unit="beantwortet",
                                bar="ratio" if rate else "whole", rate=rate)
                    verdict = _channel_verdict(channels, rate)
                    if verdict:
                        ui.label(verdict).classes("jd-card-sub")
                with ui.column().classes("jd-card gap-3"):
                    ui.label("Was jede Quelle bringt").classes("jd-card-title")
                    ui.label("Anzeigen je Quelle und wie viele davon zu "
                             "einer Bewerbung wurden.").classes("jd-card-sub")
                    # …and the POPULATION here, because this panel is about
                    # what a board delivers: a long bar with a small figure
                    # beside it is the finding — most postings, fewest
                    # applications.
                    _share_rows(sources, unit="Bewerbungen", bar="whole",
                                rate=False)
                    ui.label("Nur die Bewerbungen, die JobDeck eingetragen "
                             "hat — eine von Hand übernommene Zeile kennt "
                             "keine Quelle.").classes("jd-card-sub")

        def _share_rows(shares: list[register.Share], unit: str, bar: str,
                        rate: bool) -> None:
            """`bar` names what the bar measures — 'ratio' or 'whole'.

            Named at the call site rather than assumed, because the two panels
            drawn with this helper compare different things, and a bar whose
            meaning has to be inferred from its neighbours is a bar that will
            eventually be read as the wrong one.
            """
            widths = register.bar_widths(shares, bar)
            with ui.element("div").classes("jd-wait"):
                for index, (share, width) in enumerate(zip(shares, widths,
                                                           strict=True)):
                    last = " last" if index == len(shares) - 1 else ""
                    ui.label(f"{share.label} · {share.whole}") \
                        .classes("firma" + last)
                    with ui.element("span").classes("jd-bar" + last) \
                            .style("align-self:center"):
                        ui.element("i").style(f"width:{round(width * 100)}%")
                    ui.label(f"{share.part} {unit}"
                             + (f" · {round(share.ratio * 100)} %" if rate
                                else "")).classes("age" + last)

        # ------------------------------------------------------------------
        # 6. The register itself
        # ------------------------------------------------------------------
        def visible(view: dict) -> list[dict]:
            needle = fold(state["query"])
            rows = []
            for app_row in view["apps"]:
                if state["status"] != VIEW_ALL \
                        and str(app_row.get("status") or "") != state["status"]:
                    continue
                if needle:
                    hay = fold(" ".join(str(app_row.get(key) or "")
                                        for key in SEARCH_FIELDS))
                    if needle not in hay:
                        continue
                rows.append(app_row)
            return rows

        def draw_register(view: dict, today: datetime.date) -> None:
            rows = visible(view)
            ages = {row.bewerbung_id: row
                    for row in register.silence(view["apps"],
                                                view["follow_up_days"], today)}
            with ui.column().classes("jd-card gap-2"):
                with ui.row().classes("w-full items-baseline gap-3"):
                    # Not "Die Firmen": the figure counts ledger ROWS, and
                    # one company can hold two of them whenever the duplicate
                    # gate's normaliser reads two spellings as different.
                    ui.label("Die Bewerbungen").classes("jd-card-title")
                    ui.label(f"{len(rows)} von {len(view['apps'])}"
                             if len(rows) != len(view["apps"])
                             else f"{len(rows)}").classes("jd-card-sub")
                with ui.element("div").classes("jd-head"):
                    # NOT "Still": four nouns and one English-looking
                    # adjective, above cells reading "12 T" — it parsed as
                    # "still 12".
                    for column in ("Firma", "Gesendet", "Kanal", "Status",
                                   "Antwort", "Wartet seit"):
                        ui.label(column)
                if not rows:
                    ui.label("Keine Bewerbung passt zu dieser Suche.") \
                        .classes("jd-card-sub")
                for app_row in rows:
                    row_id = int(app_row["id"] or 0)
                    _register_row(app_row, ages.get(row_id),
                                  view["answer_dates"].get(row_id, ""))

        def _register_row(app_row: dict, age: register.Waiting | None,
                          answered_at: str = "") -> None:
            row_id = int(app_row["id"])
            status = str(app_row.get("status") or "")
            element = ui.element("button").classes("jd-app") \
                .mark(f"application-{row_id}")
            # The id is captured HERE, in Python. The browser never says which
            # application a click was on, so a forged event cannot name one.
            element.on("click", lambda _=None, i=row_id: open_application(i))
            with element:
                ui.label(str(app_row.get("firma") or "—")).classes("firma")
                ui.label(str(app_row.get("gesendet_am") or "—")[:10]) \
                    .classes("cell")
                ui.label(str(app_row.get("kanal") or "—")).classes("cell")
                with ui.element("div"):
                    ui.label(status or "—").classes(
                        f"jd-pill {_pill_class(status)}")
                # The day the register FIRST held an answer — the recording
                # moment for imported rows, the mail's arrival for ingested
                # ones is one history-write away from it either way.
                # A row that IS answered but whose transition predates the
                # audit trail (the old tracker's imports) reads "?" rather
                # than "—": an empty cell beside an Absage pill says the
                # application is still waiting, which is the one thing that
                # column must never say.
                ui.label(answered_at[:10] if answered_at else
                         "?" if status in BEANTWORTET_STATUS else "—") \
                    .classes("cell")
                ui.label(
                    "" if age is None
                    else f"{age.days} T" if age.days is not None else "?"
                ).classes("cell right" + (" over" if age and age.overdue else ""))

        # ------------------------------------------------------------------
        # Opening one application
        # ------------------------------------------------------------------
        async def open_application(row_id: int | None) -> None:
            # Before the read, never after: clearing this slot after an await
            # would destroy a dialog opened while the read was in flight, and
            # in NiceGUI that also drops the canary whose finalizer resolves
            # `await confirm` — so a confirmation would hang for the life of
            # the page. Housekeeping first, then the work.
            overlay.clear()
            found = await run.io_bound(_one, row_id) if row_id else None
            if row_id and found is None:
                say("Diese Bewerbung gibt es nicht mehr.", type="warning")
                await refresh()
                return
            found = found or {"row": {}, "letter": None, "history": []}
            _dialog(found["row"], found["letter"], found["history"])

        def _dialog(data: dict, letter: dict | None,
                    history: list[dict]) -> None:
            with overlay, ui.dialog() as dialog, ui.card().classes("w-[42rem]"):
                ui.label(data.get("firma") or "Neue Bewerbung") \
                    .classes("jd-card-title")
                fields = {}
                with ui.grid(columns=2).classes("w-full gap-2"):
                    fields["firma"] = ui.input(
                        "Firma", value=data.get("firma") or "") \
                        .mark("field-firma")
                    fields["email"] = ui.input(
                        "E-Mail", value=data.get("email") or "")
                    fields["ansprechpartner"] = ui.input(
                        "Ansprechpartner",
                        value=data.get("ansprechpartner") or "")
                    fields["strasse"] = ui.input(
                        "Straße", value=data.get("strasse") or "")
                    fields["plz_ort"] = ui.input(
                        "PLZ / Ort", value=data.get("plz_ort") or "")
                    fields["gesendet_am"] = ui.input(
                        "Gesendet am (JJJJ-MM-TT)",
                        # Today only for a NEW row. Pre-filling an existing
                        # dateless one meant that merely opening it and
                        # pressing Speichern replaced the "seit unbekannt" the
                        # panel above deliberately preserves with today's date.
                        value=data.get("gesendet_am")
                        or ("" if data.get("id")
                            else datetime.date.today().isoformat()))
                    kanal = ui.select(
                        KANAL_OPTIONS, label="Kanal",
                        value=data.get("kanal") or KANAL_OPTIONS[0])
                    status = ui.select(
                        STATUS_OPTIONS, label="Status",
                        value=data.get("status") or STATUS_OPTIONS[0])
                fields["notiz"] = ui.input(
                    "Notiz", value=data.get("notiz") or "").classes("w-full")

                if letter:
                    with ui.expansion("Das Anschreiben, das rausging") \
                            .classes("w-full"):
                        ui.label(
                            "per E-Mail verschickt" if letter["status"] == "sent"
                            else "mit der Bewerbungsmappe eingereicht"
                        ).classes("jd-card-sub")
                        ui.label(letter["betreff"] or "—").classes("text-sm font-bold")
                        ui.label(letter["anschreiben_body"] or "—") \
                            .classes("text-sm whitespace-pre-line")
                if history:
                    with ui.expansion("Verlauf").classes("w-full"):
                        for entry in history:
                            ui.label(
                                f"{entry['created_at'][:16]}  "
                                f"{entry['old_status'] or '—'} → "
                                f"{entry['new_status']} ({entry['source']})"
                            ).classes("text-xs")

                async def save():
                    values = {key: field.value.strip()
                              for key, field in fields.items()}
                    values["kanal"] = kanal.value
                    if not values["firma"]:
                        say("Ohne Firma geht es nicht.", type="warning")
                        return
                    error = await run.io_bound(
                        _save, data.get("id"), values, status.value)
                    if error:
                        say(error, type="warning", multi_line=True)
                        return
                    dialog.close()
                    await refresh()

                async def delete():
                    """A real application, its history and its link to the sent
                    e-mail — one unconfirmed click removed all of it."""
                    with overlay, ui.dialog() as confirm, ui.card():
                        ui.label(f"Bewerbung bei „{data.get('firma')}“ "
                                 "löschen?").classes("font-bold")
                        ui.label("Der Verlauf und die Verknüpfung zur "
                                 "gesendeten E-Mail gehen mit. Das lässt sich "
                                 "nicht rückgängig machen.").classes("text-sm")
                        with ui.row().classes("justify-end gap-2 w-full"):
                            ui.button("Abbrechen",
                                      on_click=lambda: confirm.submit(False)) \
                                .props("flat")
                            # Marked, because the dialog BEHIND this one holds
                            # a button reading "Löschen" too: an unmarked
                            # search for that word matches both, which is the
                            # same collision the Unterlagen slice hit.
                            ui.button("Löschen", icon="delete",
                                      on_click=lambda: confirm.submit(True)) \
                                .props("color=negative") \
                                .mark("confirm-delete-application")
                    confirm.open()
                    if not await confirm:
                        return
                    await run.io_bound(_delete, data["id"])
                    dialog.close()
                    await refresh()

                with ui.row().classes("w-full justify-between"):
                    with ui.row():
                        if data.get("id"):
                            ui.button("Löschen", on_click=delete) \
                                .props("flat color=negative") \
                                .mark("delete-application")
                            # The path comes from the row this server read, not
                            # from anything the browser said about it.
                            if data.get("dokument"):
                                ui.button(
                                    "Mappe öffnen",
                                    on_click=lambda p=str(data["dokument"]):
                                    open_in_system(p)).props("flat")
                    with ui.row():
                        ui.button("Abbrechen", on_click=dialog.close).props("flat")
                        ui.button("Speichern", on_click=save)
            dialog.open()

        # ------------------------------------------------------------------
        async def do_export():
            path = await run.io_bound(export.export_csv)
            say(f"Exportiert: {path}", type="positive")
            open_in_system(str(path))

        def set_query(value) -> None:
            state["query"] = value or ""
            redraw_register()

        def set_status(value) -> None:
            state["status"] = value or VIEW_ALL
            redraw_register()

        def redraw_register() -> None:
            """Only the list, so typing in the search box does not rebuild the
            five panels above it — and does not move them under the cursor."""
            view = drawn.get("view")
            if view is None:
                return
            register_host.clear()
            with register_host:
                draw_register(view, drawn["today"])

        async def refresh():
            # Last request wins: a save and the watcher's tick can be in flight
            # together, and the older result finishing second would repaint the
            # screen with rows from before the write. Same guard as the queue's.
            refresh_gen["n"] += 1
            generation = refresh_gen["n"]
            view = await run.io_bound(_load)
            if view is None or generation != refresh_gen["n"]:
                return  # going away, or superseded by a newer refresh
            live_view.mark(view["signature"])
            today = datetime.date.today()
            drawn["view"], drawn["today"] = view, today
            container.clear()
            with container:
                draw_numbers(view)
                with ui.row().classes("w-full gap-4 items-stretch no-wrap"):
                    draw_rhythm(view, today)
                    draw_silence(view, today)
                draw_shares(view)
            redraw_register()

        with header:
            ui.input("Suchen", on_change=lambda e: set_query(e.value)) \
                .props("dense clearable debounce=350").classes("w-64") \
                .mark("register-search")
            ui.select([VIEW_ALL, *STATUS_OPTIONS], value=VIEW_ALL,
                      label="Status", on_change=lambda e: set_status(e.value)) \
                .props("dense").classes("w-48")
            ui.space()
            live_host = ui.row().classes("items-center")
            ui.button("Neue Bewerbung", icon="add",
                      on_click=lambda: open_application(None)).props("outline")
            ui.button("CSV", icon="download", on_click=do_export).props("flat")

        # Every real send records an application here — from the Postausgang,
        # from auto-send and from the Läuft strip. Sending in one tab and
        # looking here in another showed nothing until a reload, and the
        # overdue colouring was computed from that same stale snapshot.
        with live_host:
            live_view = live.watch(_signature, refresh, busy=live.dialog_open)

        await refresh()
