"""Shared page frame: header and navigation drawer."""

from contextlib import contextmanager

from nicegui import ui

NAV_ITEMS = [
    ("Dashboard", "/", "dashboard"),
    ("Search profiles", "/profiles", "manage_search"),
    ("Job inbox", "/jobs", "inbox"),
    ("Review queue", "/queue", "outbox"),
    ("Applications", "/applications", "folder_shared"),
    ("Settings", "/settings", "settings"),
]


@contextmanager
def frame(title: str):
    """Standard page scaffolding: header, drawer, content container."""
    ui.colors(primary="#2f7da3")
    with ui.header().classes("items-center"):
        ui.label("JobDeck").classes("text-xl font-bold")
        ui.label(title).classes("text-sm opacity-80 ml-4")
    with ui.left_drawer(value=True).classes("bg-slate-50").props("width=210"):
        for label, path, icon in NAV_ITEMS:
            with ui.row().classes("items-center w-full"):
                ui.button(label, icon=icon, on_click=lambda p=path: ui.navigate.to(p)) \
                    .props("flat align=left no-caps").classes("w-full justify-start")
    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
        yield
