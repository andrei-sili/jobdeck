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

from fastapi.responses import RedirectResponse
from nicegui import app, run, ui

from jobdeck import claims as claims_lib
from jobdeck import db, freshness
from jobdeck.services import claims as claims_service
from jobdeck.services import polling
from jobdeck.services import unterlagen as unterlagen_service
from jobdeck.ui import live
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
    rows = db.list_jobs(con, status="new", limit=1, mismatches="exclude",
                        gone="exclude", applied="exclude", old="exclude",
                        stale_age_days=stale_age)
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
             "headline": claims_lib.headline(row)}
            for row in db.list_claims(con)
        ]
        profiles = [dict(row) for row in db.list_profiles(con)]
        global_tags = db.get_setting(con, "global_hard_tags", "")
        stale_age = db.get_setting(con, "stale_age_days", "")
    return {"signature": signature, "mappe": mappe_view, "claims": register,
            "letters": len(letters), "profiles": profiles,
            "global_hard_tags": global_tags, "stale_age_days": stale_age}


def _signature() -> tuple:
    with db.db() as con:
        return unterlagen_service.signature(con, _preview_job(con))


def _save_claim(claim_id, values):
    with db.db() as con:
        if claim_id is None:
            db.add_claim(con, values)
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
                note = f" · {result['compression']}" if result["compression"] else ""
                say(f"{result['pages']} Seiten · "
                    f"{_kb(result['size_bytes'])}{note}", type="positive")
            await refresh()

        def _current_preview_job() -> int | None:
            job = drawn.get("mappe", {}).get("preview", {}).get("job")
            return job["id"] if job else None

        def draw_mappe(view: dict) -> None:
            specimen = view["specimen"]
            with ui.column().classes("jd-card gap-3"):
                ui.label("Die Mappe, Seite für Seite").classes("jd-card-title")
                ui.label("So kommt sie an — gemessen an den Dateien, die "
                         "wirklich zusammengeheftet werden.") \
                    .classes("jd-card-sub")

                if view["anlagen_error"]:
                    ui.label(f"⚠ {view['anlagen_error']}").classes("jd-note danger")

                parts = view["parts"]
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

                if specimen["built"]:
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

                with ui.row().classes("gap-2 items-center"):
                    ui.button("Neu bauen", icon="refresh",
                              on_click=rebuild_mappe).props("flat")
                    ui.button("Ansehen", icon="picture_as_pdf",
                              on_click=lambda p=view["specimen_path"]:
                                  open_in_system(p)) \
                        .props("flat").set_enabled(specimen["built"])
                ui.label("Die Reihenfolge kommt aus den Dateinamen "
                         "(01_, 02_ …) — so blättert ein Personaler: Zeugnis "
                         "vor Zertifikaten. Umbenennen ordnet um.") \
                    .classes("jd-card-sub")

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
            for label, budget in (("E-Mail", view["target_email_bytes"]),
                                  ("Portal", view["target_portal_bytes"])):
                fits = size <= budget
                notes.append({
                    "tone": "" if fits else "warn",
                    "text": (f"{label}: {_kb(size)} von {_kb(budget)} — passt"
                             if fits else
                             f"{label}: {_kb(size)} über dem Budget von "
                             f"{_kb(budget)}; für diesen Weg wird stärker "
                             f"komprimiert."),
                })
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
                              "terms": terms.value}
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

        async def propose_claims():
            """Read profile.md once and offer what the register does not hold.

            Proposes only. The register is the list of things a letter may say
            about him, and a model's reading of his own file is not authority
            for that — every row is his to keep or drop before anything is
            written.
            """
            say("profile.md wird gelesen…")
            result = await claims_service.propose_from_profile()
            if not result["ok"]:
                say(result["error"], type="warning")
                return
            if not result["claims"]:
                skipped = result.get("skipped", 0)
                say("Nichts Neues gefunden"
                    + (f" — {skipped} stehen schon im Register" if skipped
                       else ""))
                return
            overlay.clear()
            with overlay, ui.dialog() as dialog, ui.card().classes("w-[36rem]"):
                ui.label("Vorschlag aus profile.md").classes("font-bold")
                ui.label("Nichts davon ist gespeichert. Nimm nur, was ein Brief "
                         "wirklich behaupten darf.").classes("text-sm")
                boxes = []
                for claim in result["claims"]:
                    box = ui.checkbox(claim["headline"], value=True) \
                        .classes("w-full")
                    if claim["terms"]:
                        ui.label(f"erkennbar an: {claim['terms']}") \
                            .classes("text-xs text-gray-500 ml-8")
                    boxes.append((box, claim))
                if result.get("skipped"):
                    ui.label(f"{result['skipped']} standen bereits im Register "
                             "und sind nicht dabei.") \
                        .classes("text-xs text-gray-500")
                ui.label(f"Kosten dieses Aufrufs: "
                         f"{result['cost_usd']:.4f} $").classes("text-xs "
                                                                "text-gray-500")

                async def keep():
                    chosen = [{"fact": c["fact"], "binding": c["binding"],
                               "terms": c["terms"]}
                              for box, c in boxes if box.value]
                    dialog.close()
                    written = await claims_service.accept(chosen)
                    say(f"{written} übernommen", type="positive")
                    await refresh()

                with ui.row().classes("w-full justify-end"):
                    ui.button("Verwerfen", on_click=dialog.close).props("flat")
                    ui.button("Übernehmen", on_click=keep) \
                        .mark("accept-claims")
            dialog.open()

        def draw_claims(register: list[dict], letters: int) -> None:
            with ui.column().classes("jd-card gap-3"):
                ui.label("Was ein Brief behaupten darf — und wie oft er es tat") \
                    .classes("jd-card-title")
                ui.label(f"Gezählt über {letters} geschriebene Anschreiben.") \
                    .classes("jd-card-sub")

                if not register:
                    ui.label("Noch keine Erlaubnis eingetragen. Jede Zeile ist "
                             "eine: ein Können und das eine Projekt, an dem es "
                             "hängt.").classes("jd-note")
                for claim in register:
                    with ui.element("div").classes("jd-claim"):
                        # Two labels rather than one span of markup: the text is
                        # the user's own, but a screen that renders stored text
                        # as HTML is a habit, and this app already had to unlearn
                        # it once for postings.
                        with ui.row().classes("jd-claim-fact items-baseline "
                                              "gap-1 no-wrap"):
                            ui.label(claim["fact"])
                            if claim["binding"]:
                                ui.label(f"— {claim['binding']}") \
                                    .classes("jd-claim-bind")
                        tone = ("never" if claim["uses"] == 0 else
                                "unknown" if claim["uses"] is None else "")
                        ui.label(claims_lib.describe_uses(claim["uses"])) \
                            .classes(f"jd-claim-count {tone}")
                        with ui.row().classes("gap-0 items-center no-wrap"):
                            ui.button(icon="edit",
                                      on_click=lambda c=claim: claim_dialog(c)) \
                                .props("flat round dense size=sm")
                            ui.button(icon="delete",
                                      on_click=lambda c=claim: delete_claim(c)) \
                                .props("flat round dense size=sm color=negative") \
                                .mark("delete-claim")

                with ui.row().classes("gap-2 items-center"):
                    ui.button("Erlaubnis hinzufügen", icon="add",
                              on_click=lambda: claim_dialog(None)).props("flat")
                    ui.button("Aus profile.md lesen", icon="auto_awesome",
                              on_click=propose_claims) \
                        .props("flat").mark("propose-claims") \
                        .tooltip("Ein KI-Aufruf über deine profile.md. "
                                 "Nichts wird gespeichert, bevor du es "
                                 "bestätigt hast.")
                # The register counts; it does not yet constrain. Saying so is
                # the difference between a screen and a promise — the drafting
                # prompt still reads profile.md, and a line claiming otherwise
                # would be believed at exactly the wrong moment.
                ui.label("Heute zählt dieses Register mit: es liest, was die "
                         "Briefe wirklich behauptet haben. Was die KI "
                         "behaupten DARF, steht weiterhin in profile.md.") \
                    .classes("jd-card-sub")

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
                ).tooltip("Nur Entwürfe, die du in der Prüfliste freigegeben "
                          "hast — getaktet, in Geschäftszeiten, unter dem "
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
                                     f"{profile['last_polled_at'][:16]}") \
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
                draw_preview(view["mappe"])
                draw_claims(view["claims"], view["letters"])
                draw_profiles(view)

        with header:
            live_view = live.watch(_signature, refresh, busy=live.dialog_open)
        await refresh()
