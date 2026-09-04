"""Unterlagen & Profil — what the employer gets, and what the AI may claim.

Four panels, in the order the question is actually asked. The Mappe first,
because that is the object an employer receives; then the letter head filled
with a real posting, so a missing field is found here rather than in a PDF;
then the register of what a letter is allowed to claim, with how often each
permission was used; then the search profiles that decide what arrives at all.

The Mappe is measured, never described: everything on the first panel comes
from the files on disk and from a specimen PDF built through the same code
path as a real application.
"""

import json
import pathlib

from fastapi.responses import RedirectResponse
from nicegui import app, run, ui

from jobdeck import claims as claims_lib
from jobdeck import config, db, freshness
from jobdeck.ai import profile as ai_profile
from jobdeck.services import anlagen as anlagen_service
from jobdeck.services import claims as claims_service
from jobdeck.services import polling
from jobdeck.services import unterlagen as unterlagen_service
from jobdeck.ui import live, rail
from jobdeck.ui.helpers import open_in_system
from jobdeck.ui.layout import frame
from jobdeck.ui.rail import UNTERLAGEN_PATH

ALL_SOURCES = ["arbeitsagentur", "jooble", "arbeitnow"]


def _kb(size: int) -> str:
    """A weight a person can compare at a glance, never more precise than the
    decision it supports."""
    if size <= 0:
        return "—"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1024 / 1024:.1f} MB".replace(".", ",")


def _pages(count: int) -> str:
    return "1 Seite" if count == 1 else f"{count} Seiten"


def _preview_job(con) -> int | None:
    """The posting the letter head is filled with: the best one still open.

    Deliberately a REAL posting rather than a placeholder. The panel exists to
    show which fields an actual application would leave empty, and on his
    corpus most postings state no postal address at all — a specimen with
    invented values would report a completeness the app does not have.
    """
    stale_age = freshness.stale_age_setting(
        db.get_setting(con, "stale_age_days", ""))
    # `sort="score"` explicitly: the list default became "newest first" for
    # the Stellen screen, and this panel wants the BEST open posting — a
    # preview filled from whatever arrived last says less about whether his
    # letter head is complete.
    rows = db.list_jobs(con, status="new", limit=1, mismatches="exclude",
                        gone="exclude", applied="exclude", old="exclude",
                        hidden="exclude", stale_age_days=stale_age,
                        sort="score")
    return rows[0]["id"] if rows else None


def _load() -> dict:
    """Everything the screen states, on one short-lived connection."""
    with db.db() as con:
        job_id = _preview_job(con)
        # First, before anything it labels: sqlite3 gives every SELECT its own
        # snapshot, so a write landing between them would marry stale rows to a
        # fresh signature, which the watcher then records as what is showing.
        signature = unterlagen_service.signature(con, job_id)
        mappe_view = unterlagen_service.read(con, job_id)
        letters = db.letter_bodies(con)
        register = [
            {**dict(row),
             "uses": claims_lib.count_uses(row["terms"], letters),
             "headline": claims_lib.headline(row),
             "provenance": claims_lib.provenance(row)}
            for row in db.list_claims(con)
        ]
        refused = [{**dict(row), "provenance": claims_lib.provenance(row)}
                   for row in db.list_claims(con, states=("rejected",))]
        coverage = claims_lib.coverage(
            claims_lib.profile_sections(ai_profile.load_profile()), register)
        profiles = [dict(row) for row in db.list_profiles(con)]
        global_tags = db.get_setting(con, "global_hard_tags", "")
        stale_age = db.get_setting(con, "stale_age_days", "")
    return {"signature": signature, "mappe": mappe_view, "claims": register,
            "coverage": coverage, "refused": refused,
            "letters": len(letters), "profiles": profiles,
            "global_hard_tags": global_tags, "stale_age_days": stale_age}


def _signature() -> tuple:
    with db.db() as con:
        return unterlagen_service.signature(con, _preview_job(con))


# Every write into the Anlagen folder answers with {"ok": …} rather than
# raising. An exception out of a NiceGUI handler is a line in the log and
# nothing on screen, so a refused upload would look exactly like a successful
# one — and this is the screen whose whole complaint was "I do not understand
# what to do".
# OSError is caught beside AnlagenError, not instead of it. The service turns
# every filesystem refusal it can foresee into a sentence, but "every one it
# can foresee" is the part that ages: a mount that goes read-only between two
# lines, a name the kernel dislikes. Whatever is left must still come back as
# a message, because the alternative is a log line and a button that looks
# alive and does nothing.
def _add_anlage(folder: str, filename: str, data: bytes) -> dict:
    try:
        stored = anlagen_service.store(pathlib.Path(folder), filename, data)
    except (anlagen_service.AnlagenError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "name": stored.name}


def _move_anlage(folder: str, name: str, delta: int) -> dict:
    try:
        anlagen_service.move(pathlib.Path(folder), name, delta)
    except (anlagen_service.AnlagenError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def _remove_anlage(folder: str, name: str) -> dict:
    try:
        landed = anlagen_service.remove(pathlib.Path(folder), name)
    except (anlagen_service.AnlagenError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "landed": str(landed)}


def _adopt_folder(path: str) -> dict:
    """Create the Anlagen folder and make it the configured one.

    The one press that answers "where do I put my documents" for somebody who
    has never opened Einstellungen. It is also the repair for a folder that
    was moved: the same button re-creates the path the setting already names,
    rather than silently pointing him somewhere else.
    """
    folder = config.user_path(path)
    if folder is None:
        return {"ok": False, "error":
                f"Mit diesem Pfad kann nichts angefangen werden: {path}"}
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"Ordner konnte nicht angelegt werden: {exc}"}
    with db.db() as con:
        db.set_setting(con, "anlagen_dir", str(folder))
    return {"ok": True, "path": str(folder)}


def _save_claim(claim_id, values):
    with db.db() as con:
        if claim_id is None:
            # Typed into the register's own form, so it is his word and needs
            # no second confirmation. Every other way in leaves a proposal.
            db.add_claim(con, {**values, "state": "confirmed"})
        else:
            db.update_claim(con, claim_id, values)


def _delete_claim(claim_id):
    with db.db() as con:
        db.delete_claim(con, claim_id)


def _save_profile(profile_id, values):
    with db.db() as con:
        if profile_id is None:
            db.add_profile(con, values)
        else:
            db.update_profile(con, profile_id, values)


def _delete_profile(profile_id):
    with db.db() as con:
        db.delete_profile(con, profile_id)


def _get_profile_row(profile_id):
    with db.db() as con:
        rows = db.list_profiles(con)
    return next(r for r in rows if r["id"] == profile_id)


@app.get("/profiles")
def legacy_profiles_page():
    """The search profiles' old address. They are one panel of this screen
    now — a search profile decides what arrives, which is a question about
    the documents, not a page of its own."""
    return RedirectResponse(UNTERLAGEN_PATH)


@ui.page(UNTERLAGEN_PATH)
async def unterlagen_page():
    async with frame("Unterlagen & Profil", current="unterlagen"):
        header = ui.row().classes("w-full items-center gap-2")
        container = ui.column().classes("w-full gap-4")
        # Dialogs and messages live in a sibling of the list, never in the row
        # that opened them: a handler runs in the slot of its own button and
        # refresh() clears `container`, so a coroutine parked on `await confirm`
        # would never resolve once the page rebuilt itself — and this page
        # rebuilds on a timer, so that can happen without anyone clicking.
        overlay = ui.column().classes("contents")
        # Whether the refused pile is open. A view he opened is not DATA, so
        # it stays out of the page signature — putting it there would make
        # the watcher rebuild the screen underneath him every time he looked.
        showing = {"refused": False}

        def say(message: str, **kwargs) -> None:
            """Tell the user something, from a slot no refresh can delete."""
            with overlay:
                ui.notify(message, **kwargs)

        # ------------------------------------------------------------------
        # 1. The Mappe, page by page
        # ------------------------------------------------------------------
        async def rebuild_mappe():
            say("Mappe wird gebaut…")
            result = await unterlagen_service.build(_current_preview_job())
            if not result["ok"]:
                say(result["error"], type="warning")
            else:
                # `Compression.describe()` is written for the log and is
                # English; this toast lands between two German sentences.
                say(f"{result['pages']} Seiten · "
                    f"{_kb(result['size_bytes'])} — gebaut", type="positive")
                if result.get("cv_error"):
                    # the Mappe is built; the portal CV is not, and the
                    # ATS panel below says so too — but a toast is what he
                    # is looking at when he pressed
                    say(result["cv_error"], type="warning")
            await refresh()

        def _current_preview_job() -> int | None:
            job = drawn.get("mappe", {}).get("preview", {}).get("job")
            return job["id"] if job else None

        def _folder() -> dict:
            return drawn.get("mappe", {}).get("folder", {})

        # ---- the Anlagen folder: getting documents in, and in order --------
        # A rebuild clears `container`, which is where the Quasar uploader
        # lives — and unmounting it mid-transfer aborts every file still on the
        # wire AND unregisters the POST route they were using. With
        # `multiple=True` that is not a corner case: dropping six certificates
        # at once means the smallest lands, its handler refreshes, and the
        # other five die in silence with a green toast on screen.
        #
        # So a batch is one unit, and it is handled by ONE callback. NiceGUI
        # schedules an async event handler as a background task rather than
        # awaiting it, so `on_upload` and `on_multi_upload` do not run in
        # order — a "store each, refresh at the end" split would race its own
        # refresh past the last file. With Quasar's `batch`, the whole
        # selection arrives as a single POST and `on_multi_upload` is the only
        # handler there is: stores in order, redraws once, says one sentence.
        batch: dict = {"active": False, "moving": False}

        def _busy() -> bool:
            return live.dialog_open() or batch["active"]

        async def add_folder() -> None:
            state = _folder()
            wanted = state.get("path") or str(anlagen_service.default_dir())
            result = await run.io_bound(_adopt_folder, wanted)
            if not result["ok"]:
                say(result["error"], type="warning", multi_line=True)
            else:
                say(f"Ordner angelegt: {result['path']}", type="positive",
                    multi_line=True)
            await refresh()

        def begin_upload() -> None:
            """The transfer has started — hold the screen still until it ends."""
            batch["active"] = True

        async def upload_anlagen(event) -> None:
            """Everything he dropped, in the order he dropped it.

            The size is asked before the bytes: `read()` pulls a file into
            memory whole, and the point of a limit is not to have held it
            first. Each file is reported by name, because "three of five
            arrived" is useless if it does not say which two did not.
            """
            try:
                folder = _folder().get("path") or ""
                done, failed = [], []
                for file in event.files:
                    if not folder:
                        failed.append("Zuerst einen Ordner für die Anlagen "
                                      "anlegen.")
                        break
                    too_big = anlagen_service.oversize_message(file.size())
                    if too_big:
                        failed.append(f"{file.name}: {too_big}")
                        continue
                    data = await file.read()
                    result = await run.io_bound(_add_anlage, folder,
                                                file.name, data)
                    if result["ok"]:
                        done.append(result["name"])
                    else:
                        failed.append(f"{file.name}: {result['error']}")
            finally:
                # Before the redraw, and whatever happened: a flag left raised
                # freezes the page's self-refresh for the rest of its life.
                batch["active"] = False
            for message in failed:
                say(message, type="warning", multi_line=True)
            if done:
                landed = ("„" + done[0] + "“ liegt" if len(done) == 1
                          else f"{len(done)} Anlagen liegen")
                # The page numbers are already right — the letter's own length
                # is remembered and every part is re-placed on this redraw. The
                # one figure that has gone stale is the WEIGHT, which is what
                # the budget lines are read for, and the panel's own note three
                # lines away says exactly that.
                say(f"{landed} jetzt in der Mappe — für Größe und Budget "
                    f"einmal „Neu bauen“.",
                    type="positive", multi_line=True)
            await refresh()

        def upload_rejected() -> None:
            """Quasar refuses on the client too; without this it refuses in
            silence and the file simply never appears."""
            say(f"Abgelehnt — nur PDF, höchstens "
                f"{anlagen_service.MAX_UPLOAD_BYTES // 1024 // 1024} MB "
                f"pro Anlage.", type="warning", multi_line=True)

        async def move_anlage(name: str, delta: int) -> None:
            """Move one Anlage, and refuse to start a second move meanwhile.

            The arrows RENAME, so between the press and the redraw the name in
            the next button's closure is already stale — and after a swap that
            name belongs to a DIFFERENT document. A second press inside that
            window would move the wrong certificate, quietly and correctly as
            far as the code is concerned. He nudges twice; the gate makes the
            second press wait for the first to be on screen.
            """
            if batch["moving"]:
                return
            batch["moving"] = True
            try:
                folder = _folder().get("path") or ""
                result = await run.io_bound(_move_anlage, folder, name, delta)
                if not result["ok"]:
                    say(result["error"], type="warning", multi_line=True)
                await refresh()
            finally:
                batch["moving"] = False

        async def remove_anlage(part) -> None:
            """Take an Anlage out of the Mappe — the file survives.

            Asked first, and the question says where the file goes. This is
            the one control on the screen that could read as "delete my
            Prüfungszeugnis", and the answer to that fear belongs in the
            question, not in a toast afterwards.
            """
            folder = _folder().get("path") or ""
            overlay.clear()
            with overlay, ui.dialog() as confirm, ui.card():
                ui.label(f"„{part.label}“ aus der Mappe nehmen?") \
                    .classes("font-bold")
                ui.label("Die Datei wird nicht gelöscht — sie wandert nach "
                         f"{anlagen_service.trash_dir()} und ist ab dann "
                         "nicht mehr Teil der Bewerbung.").classes("text-sm")
                with ui.row().classes("justify-end gap-2 w-full"):
                    ui.button("Abbrechen",
                              on_click=lambda: confirm.submit(False)).props("flat")
                    ui.button("Herausnehmen", icon="delete",
                              on_click=lambda: confirm.submit(True)) \
                        .props("color=negative").mark("confirm-remove-anlage")
            confirm.open()
            if not await confirm:
                return
            result = await run.io_bound(_remove_anlage, folder, part.name)
            if not result["ok"]:
                say(result["error"], type="warning", multi_line=True)
            else:
                say(f"„{part.label}“ liegt jetzt in {result['landed']}",
                    type="positive", multi_line=True)
            await refresh()

        def _row_actions(part, slot: int, count: int) -> None:
            """Move it, or take it out. Only for a row that IS a file.

            `slot` is the Anlage's own position among the Anlagen — not its
            position in the stack, which counts the letter and would put the
            "cannot go up" boundary one row off."""
            ui.button(icon="arrow_upward",
                      on_click=lambda p=part: move_anlage(p.name, -1)) \
                .props("flat round dense size=sm") \
                .set_enabled(slot > 0).mark(f"anlage-up-{slot}") \
                .tooltip("Nach vorn")
            ui.button(icon="arrow_downward",
                      on_click=lambda p=part: move_anlage(p.name, 1)) \
                .props("flat round dense size=sm") \
                .set_enabled(slot < count - 1).mark(f"anlage-down-{slot}") \
                .tooltip("Nach hinten")
            ui.button(icon="close", on_click=lambda p=part: remove_anlage(p)) \
                .props("flat round dense size=sm color=negative") \
                .mark(f"remove-anlage remove-anlage-{slot}") \
                .tooltip("Aus der Mappe nehmen")

        def draw_mappe(view: dict) -> None:
            specimen = view["specimen"]
            with ui.column().classes("jd-card gap-3"):
                ui.label("Die Mappe, Seite für Seite").classes("jd-card-title")
                ui.label("So kommt sie an — gemessen an den Dateien, die "
                         "wirklich zusammengeheftet werden.") \
                    .classes("jd-card-sub")

                folder = view["folder"]
                if folder["note"]:
                    tone = "danger" if folder["state"] == "missing" else "warn"
                    ui.label(folder["note"]).classes(f"jd-note {tone}")

                parts = view["parts"]
                # Which rows are Anlagen decides which may move where. Taken
                # from the file each part carries rather than from its
                # position, so the letter — which has no file — can never be
                # offered a control that would rename or remove something.
                files = [i for i, part in enumerate(parts) if part.name]
                with ui.element("div").classes("jd-stack"):
                    for index, part in enumerate(parts):
                        last = " last" if index == len(parts) - 1 else ""
                        # A position is printed only when it is known. Before
                        # the first build the letter's length is not, and
                        # everything after it would be announced three pages
                        # too early — which is the wrong kind of wrong for a
                        # number somebody checks against a printout.
                        span = ("—" if not (part.placed and part.pages)
                                else f"{part.first_page}–{part.last_page}"
                                if part.pages > 1 else str(part.first_page))
                        ui.label(span).classes("jd-pageno" + last)
                        ui.label(part.label).classes("jd-partname" + last)
                        ui.label(_pages(part.pages) if part.pages else "") \
                            .classes("jd-partmeta" + last)
                        if part.error:
                            ui.label(part.error).classes("jd-partmeta warn" + last)
                        elif part.size_bytes:
                            ui.label(_kb(part.size_bytes)) \
                                .classes("jd-partmeta" + last)
                        else:
                            ui.label("aus der Vorlage") \
                                .classes("jd-partmeta" + last)
                        with ui.row().classes("jd-partacts gap-0" + last):
                            if part.name:
                                _row_actions(part, files.index(index),
                                             len(files))

                if specimen["built"] and specimen["stale"]:
                    # The page count is still right — the letter's length was
                    # measured at build time and the Anlagen are counted fresh
                    # — but the WEIGHT belongs to a Mappe that no longer
                    # matches this stack, so it is withheld rather than
                    # reprinted as though it still held.
                    ui.label(f"{_pages(specimen['pages'])} · Gewicht unbekannt") \
                        .classes("jd-total")
                    ui.label("Die Anlagen haben sich seit dem letzten Bauen "
                             "geändert — für Größe und Budget einmal „Neu "
                             "bauen“.").classes("jd-note warn")
                elif specimen["built"]:
                    with ui.row().classes("items-baseline gap-3 w-full"):
                        ui.label(f"{_pages(specimen['pages'])} · "
                                 f"{_kb(specimen['size_bytes'])}") \
                            .classes("jd-total")
                        if view["shrunk_from_bytes"]:
                            kind = ("verlustfrei von" if view["lossless"]
                                    else "verkleinert von")
                            ui.label(f"{kind} {_kb(view['shrunk_from_bytes'])}") \
                                .classes("jd-card-sub")
                    for line in _budget_notes(view):
                        ui.label(line["text"]).classes(f"jd-note {line['tone']}")
                else:
                    ui.label("Noch nicht gebaut — die Seiten der Vorlage und "
                             "das Gesamtgewicht stehen erst danach fest.") \
                        .classes("jd-note")

                if folder["state"] in ("unset", "missing"):
                    ui.button("Ordner anlegen und verwenden", icon="create_new_folder",
                              on_click=add_folder).props("flat")
                    ui.label(f"Angelegt wird: "
                             f"{folder['path'] or anlagen_service.default_dir()}") \
                        .classes("jd-card-sub")
                else:
                    with ui.element("div").classes("jd-facts w-full"):
                        ui.label("Deine Anlagen liegen in").classes("k")
                        with ui.row().classes("items-center gap-2 min-w-0"):
                            ui.label(folder["path"]).classes("jd-path")
                            ui.button("Ordner öffnen", icon="folder_open",
                                      on_click=lambda p=folder["path"]:
                                          open_in_system(p)) \
                                .props("flat dense")
                    ui.upload(on_multi_upload=upload_anlagen,
                              on_begin_upload=lambda _e: begin_upload(),
                              on_rejected=lambda _e: upload_rejected(),
                              multiple=True, auto_upload=True,
                              max_file_size=anlagen_service.MAX_UPLOAD_BYTES,
                              label="Zeugnisse und Zertifikate hierher ziehen "
                                    "oder auswählen") \
                        .props('accept=".pdf" batch flat bordered') \
                        .classes("w-full jd-upload")
                    ui.label("Nur PDF — die Mappe wird aus PDFs "
                             "zusammengeheftet. Der Lebenslauf gehört NICHT "
                             "hierher: er steht in der Vorlage und käme sonst "
                             "zweimal in der Bewerbung an.") \
                        .classes("jd-card-sub")

                with ui.row().classes("gap-2 items-center"):
                    ui.button("Neu bauen", icon="refresh",
                              on_click=rebuild_mappe).props("flat")
                    ui.button("Ansehen", icon="picture_as_pdf",
                              on_click=lambda p=view["specimen_path"]:
                                  open_in_system(p)) \
                        .props("flat").set_enabled(specimen["built"])
                ui.label("Die Reihenfolge ist die Reihenfolge der Dateinamen "
                         "(01_, 02_ …) — so blättert ein Personaler: Zeugnis "
                         "vor Zertifikaten. Die Pfeile benennen die Dateien "
                         "um; im Ordner umbenennen tut dasselbe.") \
                    .classes("jd-card-sub")

        def draw_ats(view: dict) -> None:
            """What a Bewerbermanagementsystem's parser makes of the files,
            measured on the built PDFs. Two files, because a portal takes
            either the whole Mappe as one upload or the Lebenslauf on its
            own — and the two are built differently."""
            ats = view["ats"]
            with ui.column().classes("jd-card gap-3"):
                ui.label("ATS-Check").classes("jd-card-title")
                ui.label("So liest ein Bewerbermanagementsystem die Dateien "
                         "— gemessen an den gebauten PDFs. Ein Parser lehnt "
                         "nicht ab, er sortiert; was hier rot ist, kostet "
                         "Rang, nicht die Bewerbung.").classes("jd-card-sub")
                for label, report, hint in (
                    ("Die Mappe (als eine Datei hochgeladen)", ats["mappe"],
                     "Noch nicht gebaut — „Neu bauen“ misst sie."),
                    ("Der Lebenslauf für Portale", ats["lebenslauf"],
                     "Kein Lebenslauf für Portale eingetragen — in den "
                     "Einstellungen setzen. Bis dahin bekommt ein Formular "
                     "die Lebenslauf-Seite der Mappe."
                     if not ats["cv_configured"] else
                     "Noch nicht gebaut — „Neu bauen“ rendert und misst ihn."),
                ):
                    ui.label(label).classes("font-bold")
                    if report is None:
                        ui.label(hint).classes("jd-note")
                        continue
                    if report.error:
                        ui.label(report.error).classes("jd-note danger")
                        continue
                    ui.label(f"{_pages(report.pages)} · {_kb(report.size_bytes)}"
                             f" · {report.fonts} Schriften") \
                        .classes("jd-card-sub")
                    for check in report.checks:
                        ui.label(("✓ " if check.ok else "✗ ") + check.text) \
                            .classes("jd-note" + ("" if check.ok else " warn"))

        def _budget_notes(view: dict) -> list[dict]:
            """What the built size means for each way it can travel.

            Both budgets are stated even when both are met: the portal rung is
            the tighter one and the same document goes through either channel,
            so "fits by e-mail" alone would be half an answer.
            """
            size = view["specimen"]["size_bytes"]
            notes = []
            if size > view["max_bytes"]:
                notes.append({"tone": "danger", "text":
                              f"Über der deutschen 5-MB-Konvention "
                              f"({_kb(size)}) — eine Anlage entfernen oder "
                              f"vorher verkleinern."})
            for label, budget, harder in (
                    # The specimen is ALREADY fitted to the e-mail budget, so
                    # an over-budget e-mail line means the app has compressed
                    # as hard as it ever will — promising more there was always
                    # false. Only the portal rung is still to come, and only
                    # when shrinking is switched on at all.
                    ("E-Mail", view["target_email_bytes"], False),
                    ("Portal", view["target_portal_bytes"], view["compress"])):
                fits = size <= budget
                if fits:
                    text = f"{label}: {_kb(size)} von {_kb(budget)} — passt"
                elif harder:
                    text = (f"{label}: {_kb(size)} über dem Budget von "
                            f"{_kb(budget)}; für diesen Weg wird stärker "
                            f"komprimiert.")
                else:
                    text = (f"{label}: {_kb(size)} über dem Budget von "
                            f"{_kb(budget)} — "
                            + ("Verkleinern ist in den Einstellungen "
                               "ausgeschaltet, es geht so raus."
                               if not view["compress"] else
                               "die Qualitätsgrenze lässt nicht mehr zu."))
                notes.append({"tone": "" if fits else "warn", "text": text})
            return notes

        # ------------------------------------------------------------------
        # 2. The letter head, with real data
        # ------------------------------------------------------------------
        def draw_preview(view: dict) -> None:
            preview = view["preview"]
            with ui.column().classes("jd-card gap-3"):
                ui.label("Vorschau mit echten Daten").classes("jd-card-title")
                if preview["job"] is None:
                    ui.label("Keine offene Anzeige in der Arbeitsliste — die "
                             "Vorschau wird mit einer echten gefüllt, nie mit "
                             "erfundenen Werten.").classes("jd-note")
                    return
                job, values = preview["job"], preview["values"]
                ui.label(f"Gefüllt mit: {job['company']} — {job['title']}") \
                    .classes("jd-card-sub")
                with ui.element("div").classes("jd-letter"):
                    with ui.element("div").classes("addr"):
                        for key in ("firma", "ansprechpartner", "strasse",
                                    "plz_ort"):
                            text = str(values.get(key) or "").strip()
                            ui.label(text or "—") \
                                .classes("" if text else "gap")
                    ui.label(f"{values['ort']}, {values['datum']}").classes("date")
                    ui.label(values["betreff"]).classes("subj")
                    ui.label("Sehr geehrte…  — hier steht das Anschreiben, das "
                             "für jede Bewerbung neu geschrieben wird.") \
                        .classes("body")
                for gap in preview["missing"]:
                    ui.label(f"{gap['label']}: {gap['why']}") \
                        .classes("jd-note warn")
                if not preview["missing"]:
                    ui.label("Kein Feld bleibt leer.").classes("jd-note")

        # ------------------------------------------------------------------
        # 3. What a letter may claim
        # ------------------------------------------------------------------
        def claim_dialog(claim: dict | None):
            data = claim or {}
            overlay.clear()
            with overlay, ui.dialog() as dialog, ui.card().classes("w-96"):
                ui.label("Erlaubnis").classes("font-bold")
                # The family, first: it decides which heading the row appears
                # under, and without it every hand-written entry was filed as
                # a competence — so "Englisch, verhandlungssicher" stood under
                # "Technische Kenntnisse", and a family the reading got wrong
                # could not be corrected anywhere in the app.
                family = ui.select(dict(claims_lib.KINDS), label="Was für eine",
                                   value=claims_lib.normalise_kind(
                                       data.get("kind"))).classes("w-full")
                fact = ui.input("Was — z. B. Django & DRF",
                                value=data.get("fact", "")).classes("w-full")
                binding = ui.input("Wobei — das Projekt oder der Arbeitgeber",
                                   value=data.get("binding", "")).classes("w-full")
                ui.label("Ein Können darf nur bei dem Projekt auftauchen, bei "
                         "dem es hier steht.").classes("text-xs text-gray-500")
                terms = ui.input("Wörter, an denen man es im Brief erkennt",
                                 value=data.get("terms", "")).classes("w-full")
                ui.label("Komma-getrennt. Ohne sie kann nicht gezählt werden, "
                         "wie oft ein Brief es behauptet hat.") \
                    .classes("text-xs text-gray-500")

                async def save():
                    if not fact.value.strip():
                        say("Ohne „Was“ ist es keine Erlaubnis", type="warning")
                        return
                    values = {"fact": fact.value, "binding": binding.value,
                              "terms": terms.value, "kind": family.value}
                    await run.io_bound(_save_claim, data.get("id"), values)
                    dialog.close()
                    await refresh()

                with ui.row().classes("w-full justify-end"):
                    ui.button("Abbrechen", on_click=dialog.close).props("flat")
                    ui.button("Speichern", on_click=save)
            dialog.open()

        async def delete_claim(claim: dict):
            overlay.clear()
            with overlay, ui.dialog() as confirm, ui.card():
                ui.label(f"„{claim['headline']}“ löschen?").classes("font-bold")
                ui.label("Danach darf kein Brief es mehr behaupten — bereits "
                         "geschriebene Briefe bleiben, wie sie sind.") \
                    .classes("text-sm")
                with ui.row().classes("justify-end gap-2 w-full"):
                    ui.button("Abbrechen",
                              on_click=lambda: confirm.submit(False)).props("flat")
                    ui.button("Löschen", icon="delete",
                              on_click=lambda: confirm.submit(True)) \
                        .props("color=negative").mark("confirm-delete-claim")
            confirm.open()
            if not await confirm:
                return
            await run.io_bound(_delete_claim, claim["id"])
            await refresh()

        async def import_claims():
            """Read profile.md once and put what the register lacks in it.

            Proposes only. Every row it writes says where it came from and
            counts for nothing until he answers it — the register is the list
            of things a letter may say about him, and a model's reading of his
            own file is not authority for that.
            """
            # It costs money, so it asks first — the same rule the drafting
            # keyboard follows. The figure is stated BEFORE the spend, not
            # inside a summary afterwards.
            overlay.clear()
            with overlay, ui.dialog() as confirm, ui.card():
                ui.label("profile.md von der KI lesen lassen?") \
                    .classes("font-bold")
                ui.label("Ein Aufruf über deine profile.md, ungefähr zwei "
                         "Cent. Was dabei herauskommt, steht als Vorschlag "
                         "im Register — behaupten darf ein Brief es erst, "
                         "wenn du es bestätigt hast.").classes("text-sm")
                with ui.row().classes("justify-end gap-2 w-full"):
                    ui.button("Abbrechen",
                              on_click=lambda: confirm.submit(False)) \
                        .props("flat")
                    ui.button("Lesen", on_click=lambda: confirm.submit(True)) \
                        .mark("confirm-propose")
            confirm.open()
            if not await confirm:
                return

            say("profile.md wird gelesen…")
            result = await claims_service.import_from_profile()
            if not result["ok"]:
                say(result["error"], type="warning")
                return
            written = result["written"]
            waiting = result.get("waiting", 0)
            answered = result.get("answered", 0)
            # Say what each number IS. A row he has not answered and a row he
            # has are opposite states, and reporting both as "beantwortet"
            # tells him the register is finished when the shelf above it is
            # still his to work through.
            rest = []
            if waiting:
                rest.append(f"{waiting} warten schon auf dich")
            if answered:
                rest.append(f"{answered} hast du schon beantwortet")
            # What it cost, every time — including the reading that found
            # nothing. The dialog this replaced printed the figure, and a
            # spend the app makes and then does not name is the one thing
            # the meter exists to prevent.
            spent = f" · {result.get('cost_usd', 0.0):.4f} $"
            tail = (" — " + " · ".join(rest)) if rest else ""
            if not written:
                say("Nichts Neues gefunden" + tail + spent)
                return
            say(f"{claims_lib.count_proposals(written)} warten auf dich"
                + tail + spent, type="positive")
            await refresh()

        async def answer_claims(claim_ids, state, spoken):
            changed = await claims_service.answer(claim_ids, state)
            if changed:
                say(f"{changed} {spoken}", type="positive")
            else:
                # Green and "0 bestätigt" is a success message for nothing
                # happening — which is what a second press on a row somebody
                # else already answered looks like.
                say("Schon beantwortet — nichts geändert")
            await refresh()

        def draw_claim_row(claim: dict, *, waiting: bool) -> None:
            with ui.element("div").classes("jd-claim"):
                # Two labels rather than one span of markup: the text is
                # the user's own, but a screen that renders stored text
                # as HTML is a habit, and this app already had to unlearn
                # it once for postings.
                with ui.column().classes("gap-0 min-w-0"):
                    with ui.row().classes("jd-claim-fact items-baseline "
                                          "gap-1 no-wrap"):
                        ui.label(claim["fact"])
                        if claim["binding"]:
                            ui.label(f"— {claim['binding']}") \
                                .classes("jd-claim-bind")
                    ui.label(claim["provenance"]).classes("jd-claim-source")
                # The same counter on both sides of the shelf. A proposal
                # was read out of the profile the letters were ALREADY being
                # written from, so "this is in 50 of your letters" is both
                # true and the strongest reason to confirm it. Printing
                # anything else here states a count nobody performed — the
                # first draw said "noch kein Wort davon" beside a fact that
                # turned out to stand in fifty.
                tone = ("never" if claim["uses"] == 0 else
                        "unknown" if claim["uses"] is None else "")
                ui.label(claims_lib.describe_uses(claim["uses"])) \
                    .classes(f"jd-claim-count {tone}")
                with ui.row().classes("gap-0 items-center no-wrap"):
                    if waiting:
                        ui.button(
                            icon="check",
                            on_click=lambda c=claim: answer_claims(
                                [c["id"]], "confirmed", "bestätigt")) \
                            .props("flat round dense size=sm color=positive") \
                            .mark("confirm-claim")
                        ui.button(
                            icon="block",
                            on_click=lambda c=claim: answer_claims(
                                [c["id"]], "rejected", "abgelehnt")) \
                            .props("flat round dense size=sm") \
                            .mark("reject-claim")
                    ui.button(icon="edit",
                              on_click=lambda c=claim: claim_dialog(c)) \
                        .props("flat round dense size=sm")
                    if not waiting:
                        ui.button(
                            icon="delete",
                            on_click=lambda c=claim: delete_claim(c)) \
                            .props("flat round dense size=sm "
                                   "color=negative") \
                            .mark("delete-claim")

        def draw_coverage(view: dict) -> None:
            """What a letter drawing only on confirmed facts could not say.

            The register is not the factual boundary yet, and this is the
            measurement that decides when it may become one. A section of his
            profile that nothing confirmed stands for is a part of himself
            that would silently drop out on the day the boundary moves.
            """
            if not view["sections"]:
                return
            ui.label(claims_lib.describe_coverage(view)).classes("jd-card-sub")
            if view["missing"]:
                ui.label("Noch nichts bestätigt aus: "
                         + " · ".join(view["missing"])).classes("jd-card-sub")

        def draw_claims(register: list[dict], letters: int,
                        coverage: dict, refused: list[dict]) -> None:
            waiting = [c for c in register if c["state"] == "proposed"]
            settled = [c for c in register if c["state"] == "confirmed"]
            with ui.column().classes("jd-card gap-3"):
                ui.label("Was ein Brief behaupten darf — und wie oft er es tat") \
                    .classes("jd-card-title")
                ui.label(f"Gezählt über {letters} geschriebene Anschreiben.") \
                    .classes("jd-card-sub")

                if waiting:
                    ui.label(f"{claims_lib.count_proposals(len(waiting))} aus "
                             "profile.md — noch ist keiner davon bestätigt") \
                        .classes("jd-urgent-note")
                    for kind, label, rows in claims_lib.group_by_kind(waiting):
                        with ui.row().classes("items-center gap-2 w-full "
                                              "mt-2"):
                            ui.label(f"{label} · {len(rows)}") \
                                .classes("jd-claim-family")
                            ui.button(
                                "Alle bestätigen",
                                on_click=lambda r=rows: answer_claims(
                                    [c["id"] for c in r], "confirmed",
                                    "bestätigt")) \
                                .props("flat dense size=sm") \
                                .mark(f"confirm-family-{kind}")
                        for claim in rows:
                            draw_claim_row(claim, waiting=True)

                if not settled:
                    # Two different states, and the wrong sentence in the
                    # second one reads as a contradiction: "jede Zeile ist
                    # eine" printed underneath eleven visible lines.
                    ui.label(
                        "Noch nichts bestätigt — die Vorschläge oben warten "
                        "auf dich." if waiting else
                        "Noch keine Erlaubnis eingetragen. Jede Zeile ist "
                        "eine: ein Können und das eine Projekt, an dem es "
                        "hängt.").classes("jd-note")
                for _kind, label, rows in claims_lib.group_by_kind(settled):
                    ui.label(label).classes("jd-claim-family mt-2")
                    for claim in rows:
                        draw_claim_row(claim, waiting=False)

                if refused:
                    async def toggle_refused():
                        showing["refused"] = not showing["refused"]
                        await refresh()

                    async def restore(claim: dict):
                        await claims_service.restore(claim["id"])
                        say("zurückgeholt — er wartet wieder auf dich",
                            type="positive")
                        await refresh()

                    # A number under a list has to be a door. A pile that is
                    # neither in a control nor behind a number is a pile
                    # nobody can open, and this one holds facts he refused
                    # with a single click on an icon beside its opposite.
                    ui.button(
                        f"{len(refused)} abgelehnt"
                        + (" ausblenden" if showing["refused"] else " anzeigen"),
                        on_click=toggle_refused).props("flat dense size=sm") \
                        .mark("toggle-refused")
                    if showing["refused"]:
                        for claim in refused:
                            with ui.element("div").classes("jd-claim"):
                                with ui.column().classes("gap-0 min-w-0"):
                                    ui.label(claims_lib.headline(claim)) \
                                        .classes("jd-claim-fact")
                                    ui.label(claim["provenance"]) \
                                        .classes("jd-claim-source")
                                ui.label("abgelehnt") \
                                    .classes("jd-claim-count unknown")
                                ui.button("Zurückholen",
                                          on_click=lambda c=claim: restore(c)) \
                                    .props("flat dense size=sm") \
                                    .mark("restore-claim")

                with ui.row().classes("gap-2 items-center"):
                    ui.button("Erlaubnis hinzufügen", icon="add",
                              on_click=lambda: claim_dialog(None)).props("flat")
                    ui.button("Aus profile.md lesen", icon="auto_awesome",
                              on_click=import_claims) \
                        .props("flat").mark("propose-claims") \
                        .tooltip("Ein KI-Aufruf über deine profile.md. "
                                 "Was er findet, wartet als Vorschlag, "
                                 "bis du es bestätigst.")
                # The register counts; it does not yet constrain. Saying so is
                # the difference between a screen and a promise — the drafting
                # prompt still reads profile.md, and a line claiming otherwise
                # would be believed at exactly the wrong moment.
                ui.label("Heute zählt dieses Register mit: es liest, was die "
                         "Briefe wirklich behauptet haben. Was die KI "
                         "behaupten DARF, steht weiterhin in profile.md.") \
                    .classes("jd-card-sub")
                draw_coverage(coverage)

        # ------------------------------------------------------------------
        # 4. The search profile
        # ------------------------------------------------------------------
        def profile_dialog(profile: dict | None):
            data = profile or {}
            overlay.clear()
            with overlay, ui.dialog() as dialog, ui.card().classes("w-96"):
                ui.label("Suchprofil").classes("font-bold")
                name = ui.input("Name", value=data.get("name", "")).classes("w-full")
                keywords = ui.input(
                    "Suchbegriffe (z. B. Python Entwickler)",
                    value=data.get("keywords", ""),
                ).classes("w-full")
                location = ui.input(
                    "Ort (leer = ganz Deutschland)",
                    value=data.get("location", ""),
                ).classes("w-full")
                radius = ui.number(
                    "Radius km (0 = unbegrenzt)",
                    value=data.get("radius_km", 0), min=0, max=200,
                ).classes("w-full")
                selected = set(json.loads(data.get("sources", "null") or "null")
                               or ALL_SOURCES)
                boxes = {s: ui.checkbox(s, value=s in selected) for s in ALL_SOURCES}
                interval = ui.number(
                    "Suchintervall (Minuten)",
                    value=data.get("poll_interval_min", 60), min=15, max=1440,
                ).classes("w-full")
                active = ui.switch("Aktiv", value=bool(data.get("active", 1)))
                auto_send = ui.switch(
                    "Freigegebene Entwürfe automatisch senden",
                    value=bool(data.get("auto_send", 0)),
                ).tooltip("Nur Entwürfe, die du selbst freigegeben hast — "
                          "über „Prüfen und senden“ bei einer Stelle — "
                          "getaktet, in Geschäftszeiten, unter dem "
                          "Tageslimit. Standard: aus.")

                with ui.expansion("Kriterien (KI-Bewertung)").classes("w-full"):
                    hard_tags = ui.textarea(
                        "Harte Anforderungen — eine pro Zeile",
                        value=data.get("hard_tags", ""),
                    ).classes("w-full").props("dense")
                    ui.label(
                        "Eine Anzeige, die eine davon klar verletzt, bekommt "
                        "Score 0 und liegt unter „Passt nicht“ — gelöscht wird "
                        "nichts."
                    ).classes("text-xs text-gray-500")
                    soft_prefs = ui.textarea(
                        "Gewichtete Wünsche — z. B. Gehalt 45000 @80%",
                        value=data.get("soft_preferences", ""),
                    ).classes("w-full").props("dense")
                    ui.label("Strenge bei verwandtem Stack").classes("text-sm mt-2")
                    strictness = ui.slider(
                        min=0, max=100, value=data.get("strictness", 50),
                    ).props("label")
                    ui.label("0 = verwandte Stacks kaum bestraft · 100 = nur "
                             "der exakte Stack zählt.") \
                        .classes("text-xs text-gray-500")

                async def save():
                    if not name.value.strip() or not keywords.value.strip():
                        say("Name und Suchbegriffe sind Pflicht", type="warning")
                        return
                    values = {
                        "name": name.value.strip(),
                        "keywords": keywords.value.strip(),
                        "location": location.value.strip(),
                        "radius_km": int(radius.value or 0),
                        "sources": [s for s, box in boxes.items() if box.value],
                        "active": active.value,
                        "auto_send": int(auto_send.value),
                        "poll_interval_min": int(interval.value or 60),
                        "hard_tags": hard_tags.value.strip(),
                        "soft_preferences": soft_prefs.value.strip(),
                        "strictness": int(strictness.value
                                          if strictness.value is not None else 50),
                    }
                    await run.io_bound(_save_profile, data.get("id"), values)
                    dialog.close()
                    await refresh()

                with ui.row().classes("w-full justify-end"):
                    ui.button("Abbrechen", on_click=dialog.close).props("flat")
                    ui.button("Speichern", on_click=save)
            dialog.open()

        async def run_now(profile_id: int):
            """A poll takes tens of seconds — long enough for the scheduler's
            own pass to finish and rebuild this list underneath. The result then
            has to be said from a slot that survives that."""
            say("Suche läuft…")
            row = await run.io_bound(_get_profile_row, profile_id)
            counters = await polling.poll_profile(row)
            say(f"Fertig: {counters['new']} neu, {counters['duplicate']} schon "
                f"beworben, {counters['known']} bekannt", type="positive")
            await refresh()

        async def delete_profile(profile: dict):
            """One unconfirmed click used to remove a search profile — and the
            postings it discovered keep referencing it."""
            overlay.clear()
            with overlay, ui.dialog() as confirm, ui.card():
                ui.label(f"Suchprofil „{profile['name']}“ löschen?") \
                    .classes("font-bold")
                ui.label("Die gefundenen Stellen bleiben erhalten; gesucht "
                         "wird damit nicht mehr.").classes("text-sm")
                with ui.row().classes("justify-end gap-2 w-full"):
                    ui.button("Abbrechen",
                              on_click=lambda: confirm.submit(False)).props("flat")
                    ui.button("Löschen", icon="delete",
                              on_click=lambda: confirm.submit(True)) \
                        .props("color=negative").mark("confirm-delete-profile")
            confirm.open()
            if not await confirm:
                return
            await run.io_bound(_delete_profile, profile["id"])
            await refresh()

        def draw_profiles(view: dict) -> None:
            profiles = view["profiles"]
            with ui.column().classes("jd-card gap-3"):
                ui.label("Suchprofil").classes("jd-card-title")
                ui.label("Was überhaupt ins Haus kommt.").classes("jd-card-sub")

                if not profiles:
                    ui.label("Noch kein Suchprofil — ohne eines wird nichts "
                             "gefunden.").classes("jd-note")
                for profile in profiles:
                    with ui.element("div").classes("jd-facts w-full py-1"):
                        where = profile["location"] or "ganz Deutschland"
                        if profile["location"] and profile["radius_km"]:
                            where += f" +{profile['radius_km']} km"
                        rows = [
                            ("Ich suche", profile["keywords"]),
                            ("Wo", where),
                            ("Quellen", ", ".join(json.loads(profile["sources"]))),
                        ]
                        if profile["hard_tags"]:
                            rows.append(("Niemals",
                                         " · ".join(profile["hard_tags"].split("\n"))))
                        if profile["soft_preferences"]:
                            rows.append(("Toleranz", profile["soft_preferences"]))
                        for key, value in rows:
                            ui.label(key).classes("k")
                            ui.label(value)
                    with ui.row().classes("items-center gap-2 w-full"):
                        title = profile["name"] + ("" if profile["active"]
                                                   else " (inaktiv)")
                        ui.label(title).classes("font-bold")
                        if profile["last_poll_error"]:
                            ui.label(f"⚠ {profile['last_poll_error']}") \
                                .classes("jd-reason")
                        elif profile["last_polled_at"]:
                            ui.label(f"zuletzt gesucht "
                                     f"{rail.clock(profile['last_polled_at'])}") \
                                .classes("jd-card-sub")
                        ui.button(icon="play_arrow",
                                  on_click=lambda p=profile: run_now(p["id"])) \
                            .props("flat round dense").tooltip("Jetzt suchen")
                        ui.button(icon="edit",
                                  on_click=lambda p=profile: profile_dialog(p)) \
                            .props("flat round dense").tooltip("Ändern")
                        ui.button(icon="delete",
                                  on_click=lambda p=profile: delete_profile(p)) \
                            .props("flat round dense color=negative") \
                            .mark("delete-profile") \
                            .tooltip("Suchprofil löschen")
                    ui.element("div").classes("w-full border-b")

                with ui.element("div").classes("jd-facts w-full"):
                    ui.label("Immer").classes("k")
                    ui.label(" · ".join(view["global_hard_tags"].split("\n"))
                             or "keine übergreifenden Anforderungen")
                    ui.label("Zu alt ab").classes("k")
                    ui.label(f"{freshness.stale_age_setting(view['stale_age_days'])} "
                             f"Tagen")
                ui.button("Suchprofil hinzufügen", icon="add",
                          on_click=lambda: profile_dialog(None)).props("flat")

        # ------------------------------------------------------------------
        drawn: dict = {}

        async def refresh() -> None:
            view = await run.io_bound(_load)
            if view is None:
                return  # the page is going away; nothing left to draw on
            live_view.mark(view["signature"])
            drawn.clear()
            drawn.update(view)
            container.clear()
            with container:
                ui.label("Was der Arbeitgeber bekommt — und woraus die KI es "
                         "bauen darf.").classes("jd-card-sub")
                draw_mappe(view["mappe"])
                draw_ats(view["mappe"])
                draw_preview(view["mappe"])
                draw_claims(view["claims"], view["letters"],
                            view["coverage"], view["refused"])
                draw_profiles(view)

        with header:
            live_view = live.watch(_signature, refresh, busy=_busy)
        await refresh()
