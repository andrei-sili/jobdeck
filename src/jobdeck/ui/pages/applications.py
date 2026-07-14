"""Applications list: the ported tracker with search, filters, and editing."""

import datetime

from nicegui import run, ui

from jobdeck import db, export
from jobdeck.constants import (
    DB_COLUMNS,
    FAELLIG_COLOR,
    KANAL_OPTIONS,
    OFFENE_STATUS,
    STATUS_COLORS,
    STATUS_OPTIONS,
)
from jobdeck.dates import days_since
from jobdeck.dedupe import find_duplicate_bewerbung, norm
from jobdeck.ui.helpers import open_in_system
from jobdeck.ui.layout import frame

SEARCH_FIELDS = ("firma", "email", "ansprechpartner", "plz_ort")


def _load():
    with db.db() as con:
        return [dict(r) for r in db.list_bewerbungen(con)]


def _save(row_id, values, new_status):
    with db.db() as con:
        dup = find_duplicate_bewerbung(
            con, values["firma"], values["email"], exclude_id=row_id
        )
        if dup is not None:
            return f"Duplicate: already applied at {dup['firma']} on {dup['gesendet_am']}"
        if row_id is None:
            db.add_bewerbung(con, {**values, "status": new_status})
        else:
            db.update_bewerbung(con, row_id, values)
            db.set_status(con, row_id, new_status, source="user")
    return None


def _delete(row_id):
    with db.db() as con:
        db.delete_bewerbung(con, row_id)


def _history(row_id):
    with db.db() as con:
        return [dict(r) for r in db.list_status_history(con, row_id)]


def _export_csv():
    return export.export_csv()


@ui.page("/applications")
async def applications_page():
    with frame("Applications"):
        state = {"query": "", "status": "Alle", "rows": []}

        columns = [
            {"name": key, "label": label, "field": key, "align": "left", "sortable": True}
            for key, label in DB_COLUMNS
        ]
        with ui.row().classes("items-center gap-4 w-full"):
            ui.input("Search", on_change=lambda e: apply_filter(query=e.value)) \
                .props("dense clearable").classes("w-64")
            ui.select(["Alle", *STATUS_OPTIONS], value="Alle", label="Status",
                      on_change=lambda e: apply_filter(status=e.value)) \
                .props("dense").classes("w-44")
            ui.space()
            count_label = ui.label("").classes("text-sm text-gray-500")
            ui.button("CSV export", icon="download", on_click=lambda: do_export()) \
                .props("outline")

        table = ui.table(columns=columns, rows=[], row_key="id",
                         pagination=15).classes("w-full")
        table.add_slot(
            "body-cell-status",
            """
            <q-td key="status" :props="props">
                <q-badge :style="{backgroundColor: props.row._color, color: '#333'}">
                    {{ props.row.status }}
                </q-badge>
            </q-td>
            """,
        )
        table.on("rowClick", lambda e: edit_dialog(e.args[1]))

        def matches(row):
            q = norm(state["query"])
            if q:
                haystack = norm(" ".join(str(row.get(k) or "") for k in SEARCH_FIELDS))
                if q not in haystack:
                    return False
            if state["status"] != "Alle" and (row.get("status") or "") != state["status"]:
                return False
            return True

        def row_color(row):
            if (row.get("status") or "") in OFFENE_STATUS:
                ds = days_since(row.get("gesendet_am") or "")
                if ds is not None and ds >= 14:
                    return FAELLIG_COLOR
            return STATUS_COLORS.get(row.get("status") or "", "#ffffff")

        async def refresh():
            state["rows"] = await run.io_bound(_load)
            apply_filter()

        def apply_filter(query=None, status=None):
            if query is not None:
                state["query"] = query
            if status is not None:
                state["status"] = status
            visible = [
                {**r, "_color": row_color(r)} for r in state["rows"] if matches(r)
            ]
            table.rows = visible
            table.update()
            total = len(state["rows"])
            shown = len(visible)
            count_label.text = (
                f"{total} applications" if shown == total else f"{shown} of {total} shown"
            )

        def edit_dialog(row: dict | None):
            data = dict(row or {})
            with ui.dialog() as dialog, ui.card().classes("w-[560px]"):
                ui.label("Application").classes("font-bold")
                fields = {}
                with ui.grid(columns=2).classes("w-full gap-2"):
                    fields["firma"] = ui.input("Firma", value=data.get("firma") or "")
                    fields["email"] = ui.input("E-Mail", value=data.get("email") or "")
                    fields["ansprechpartner"] = ui.input(
                        "Ansprechpartner", value=data.get("ansprechpartner") or "")
                    fields["strasse"] = ui.input("Straße", value=data.get("strasse") or "")
                    fields["plz_ort"] = ui.input("PLZ Ort", value=data.get("plz_ort") or "")
                    fields["gesendet_am"] = ui.input(
                        "Gesendet am (YYYY-MM-DD)",
                        value=data.get("gesendet_am")
                        or datetime.date.today().isoformat(),
                    )
                    kanal = ui.select(KANAL_OPTIONS, label="Kanal",
                                      value=data.get("kanal") or KANAL_OPTIONS[0])
                    status = ui.select(STATUS_OPTIONS, label="Status",
                                       value=data.get("status") or STATUS_OPTIONS[0])
                fields["notiz"] = ui.input("Notiz", value=data.get("notiz") or "") \
                    .classes("w-full")

                if data.get("id"):
                    with ui.expansion("Status history").classes("w-full"):
                        history_box = ui.column().classes("gap-0")

                        async def load_history():
                            entries = await run.io_bound(_history, data["id"])
                            with history_box:
                                for h in entries:
                                    ui.label(
                                        f"{h['created_at'][:16]}  "
                                        f"{h['old_status'] or '—'} → {h['new_status']} "
                                        f"({h['source']})"
                                    ).classes("text-xs")

                        ui.timer(0.1, load_history, once=True)

                async def save():
                    values = {k: f.value.strip() for k, f in fields.items()}
                    values["kanal"] = kanal.value
                    if not values["firma"]:
                        ui.notify("Firma is required", type="warning")
                        return
                    error = await run.io_bound(
                        _save, data.get("id"), values, status.value)
                    if error:
                        ui.notify(error, type="warning")
                        return
                    dialog.close()
                    await refresh()

                async def delete():
                    await run.io_bound(_delete, data["id"])
                    dialog.close()
                    await refresh()

                with ui.row().classes("w-full justify-between"):
                    with ui.row():
                        if data.get("id"):
                            ui.button("Delete", on_click=delete) \
                                .props("flat color=negative")
                            if data.get("dokument"):
                                ui.button(
                                    "Open document",
                                    on_click=lambda: open_in_system(data["dokument"]),
                                ).props("flat")
                    with ui.row():
                        ui.button("Cancel", on_click=dialog.close).props("flat")
                        ui.button("Save", on_click=save)
            dialog.open()

        async def do_export():
            path = await run.io_bound(_export_csv)
            ui.notify(f"Exported: {path}", type="positive")
            open_in_system(str(path))

        with ui.row():
            ui.button("New application", icon="add", on_click=lambda: edit_dialog(None))

        await refresh()
