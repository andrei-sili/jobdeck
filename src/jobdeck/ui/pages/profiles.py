"""Search profile management: CRUD plus on-demand polling."""

import json

from nicegui import run, ui

from jobdeck import db
from jobdeck.services import polling
from jobdeck.ui.layout import frame

ALL_SOURCES = ["arbeitsagentur", "jooble", "arbeitnow"]


def _load_profiles():
    with db.db() as con:
        return [dict(r) for r in db.list_profiles(con)]


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


@ui.page("/profiles")
async def profiles_page():
    with frame("Search profiles"):
        container = ui.column().classes("w-full gap-2")

        def edit_dialog(profile: dict | None):
            data = profile or {}
            with ui.dialog() as dialog, ui.card().classes("w-96"):
                ui.label("Search profile").classes("font-bold")
                name = ui.input("Name", value=data.get("name", "")).classes("w-full")
                keywords = ui.input(
                    "Keywords (e.g. Python Entwickler)",
                    value=data.get("keywords", ""),
                ).classes("w-full")
                location = ui.input(
                    "Location (empty = all of Germany)",
                    value=data.get("location", ""),
                ).classes("w-full")
                radius = ui.number(
                    "Radius km (0 = unlimited)",
                    value=data.get("radius_km", 0), min=0, max=200,
                ).classes("w-full")
                selected = set(json.loads(data.get("sources", "null") or "null")
                               or ALL_SOURCES)
                boxes = {
                    s: ui.checkbox(s, value=s in selected) for s in ALL_SOURCES
                }
                interval = ui.number(
                    "Poll interval (minutes)",
                    value=data.get("poll_interval_min", 60), min=15, max=1440,
                ).classes("w-full")
                active = ui.switch("Active", value=bool(data.get("active", 1)))

                with ui.expansion("Match criteria (AI scoring)").classes("w-full"):
                    hard_tags = ui.textarea(
                        "Hard requirements — one per line or comma-separated, "
                        "e.g. #backend",
                        value=data.get("hard_tags", ""),
                    ).classes("w-full").props("dense")
                    ui.label(
                        "A posting that clearly violates one gets score 0 and "
                        "is hidden behind the inbox's mismatch toggle — never "
                        "deleted."
                    ).classes("text-xs text-gray-500")
                    soft_prefs = ui.textarea(
                        "Weighted preferences — e.g. Gehalt 45000 @80%",
                        value=data.get("soft_preferences", ""),
                    ).classes("w-full").props("dense")
                    ui.label(
                        "Preferences shift the score by their weight; a "
                        "posting that doesn't mention them stays neutral."
                    ).classes("text-xs text-gray-500")
                    ui.label("Strictness for adjacent tech").classes(
                        "text-sm mt-2"
                    )
                    strictness = ui.slider(
                        min=0, max=100, value=data.get("strictness", 50),
                    ).props("label")
                    ui.label(
                        "0 = adjacent stacks barely penalized · 100 = only "
                        "the exact stack scores high."
                    ).classes("text-xs text-gray-500")

                async def save():
                    if not name.value.strip() or not keywords.value.strip():
                        ui.notify("Name and keywords are required", type="warning")
                        return
                    values = {
                        "name": name.value.strip(),
                        "keywords": keywords.value.strip(),
                        "location": location.value.strip(),
                        "radius_km": int(radius.value or 0),
                        "sources": [s for s, box in boxes.items() if box.value],
                        "active": active.value,
                        "auto_send": data.get("auto_send", 0),
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
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    ui.button("Save", on_click=save)
            dialog.open()

        async def run_now(profile_id: int):
            ui.notify("Polling sources…")
            row = await run.io_bound(_get_profile_row, profile_id)
            counters = await polling.poll_profile(row)
            ui.notify(
                f"Done: {counters['new']} new, {counters['duplicate']} already applied, "
                f"{counters['known']} known",
                type="positive",
            )
            await refresh()

        async def delete(profile_id: int):
            await run.io_bound(_delete_profile, profile_id)
            await refresh()

        async def refresh():
            container.clear()
            profiles = await run.io_bound(_load_profiles)
            with container:
                if not profiles:
                    ui.label(
                        "No search profiles yet — create one to start discovering jobs."
                    ).classes("text-gray-500")
                for p in profiles:
                    with ui.card().classes("w-full"):
                        with ui.row().classes("items-center w-full gap-4"):
                            with ui.column().classes("gap-0 grow"):
                                title = p["name"] + ("" if p["active"] else " (inactive)")
                                ui.label(title).classes("font-bold")
                                where = p["location"] or "all of Germany"
                                if p["location"] and p["radius_km"]:
                                    where += f" +{p['radius_km']} km"
                                ui.label(
                                    f"„{p['keywords']}“ · {where} · "
                                    f"{', '.join(json.loads(p['sources']))} · "
                                    f"every {p['poll_interval_min']} min"
                                ).classes("text-sm text-gray-600")
                                criteria = []
                                if p["hard_tags"]:
                                    criteria.append(
                                        "hard: " + " ".join(p["hard_tags"].split())
                                    )
                                if p["soft_preferences"]:
                                    criteria.append(
                                        "prefs: "
                                        + " ".join(p["soft_preferences"].split())
                                    )
                                if p["strictness"] != 50:
                                    criteria.append(f"strictness {p['strictness']}")
                                if criteria:
                                    ui.label(" · ".join(criteria)).classes(
                                        "text-xs text-gray-500"
                                    )
                                if p["last_polled_at"]:
                                    ui.label(f"Last poll: {p['last_polled_at'][:16]}") \
                                        .classes("text-xs text-gray-400")
                                if p["last_poll_error"]:
                                    ui.label(f"⚠ {p['last_poll_error']}") \
                                        .classes("text-xs text-red-600")
                            ui.button(icon="play_arrow",
                                      on_click=lambda pid=p["id"]: run_now(pid)) \
                                .props("flat round").tooltip("Run now")
                            ui.button(icon="edit",
                                      on_click=lambda row=p: edit_dialog(row)) \
                                .props("flat round").tooltip("Edit")
                            ui.button(icon="delete",
                                      on_click=lambda pid=p["id"]: delete(pid)) \
                                .props("flat round color=negative").tooltip("Delete")

        ui.button("New profile", icon="add", on_click=lambda: edit_dialog(None))
        await refresh()
