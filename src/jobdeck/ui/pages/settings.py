"""Settings: paths, credentials status, tunables, maintenance actions."""

from nicegui import run, ui

from jobdeck import backup, config, db
from jobdeck.services import polling
from jobdeck.ui.helpers import open_in_system
from jobdeck.ui.layout import frame


def _get_settings():
    with db.db() as con:
        return {
            "follow_up_days": db.get_setting(con, "follow_up_days", "14"),
            "daily_send_cap": db.get_setting(con, "daily_send_cap", "15"),
        }


def _set_setting(key, value):
    with db.db() as con:
        db.set_setting(con, key, value)


@ui.page("/settings")
async def settings_page():
    with frame("Settings"):
        settings = await run.io_bound(_get_settings)

        with ui.card().classes("w-full"):
            ui.label("Data & credentials").classes("font-bold")
            ui.label(f"Data directory: {config.DATA_DIR}").classes("text-sm")
            with ui.row().classes("items-center gap-2"):
                ui.button("Open data folder", icon="folder_open",
                          on_click=lambda: open_in_system(str(config.DATA_DIR))) \
                    .props("outline dense")
            for label, present in [
                ("Jooble API key", bool(config.jooble_api_key())),
                ("Anthropic API key", bool(config.anthropic_api_key())),
                ("Gmail OAuth (client_secret.json)", config.CLIENT_SECRET_PATH.exists()),
                ("Gmail connected (token.json)", config.TOKEN_PATH.exists()),
            ]:
                icon = "check_circle" if present else "cancel"
                color = "text-green-600" if present else "text-red-500"
                with ui.row().classes("items-center gap-1"):
                    ui.icon(icon).classes(color)
                    ui.label(label).classes("text-sm")
            ui.label(
                f"Keys are read from {config.ENV_PATH} (see .env.example in the repo)."
            ).classes("text-xs text-gray-500")

        with ui.card().classes("w-full"):
            ui.label("Tunables").classes("font-bold")
            follow_up = ui.number("Follow-up reminder after (days)",
                                  value=int(settings["follow_up_days"]),
                                  min=1, max=365).classes("w-64")
            cap = ui.number("Daily send cap (Phase 2)",
                            value=int(settings["daily_send_cap"]),
                            min=1, max=100).classes("w-64")

            async def save():
                await run.io_bound(_set_setting, "follow_up_days",
                                   str(int(follow_up.value or 14)))
                await run.io_bound(_set_setting, "daily_send_cap",
                                   str(int(cap.value or 15)))
                ui.notify("Saved", type="positive")

            ui.button("Save", on_click=save)

        with ui.card().classes("w-full"):
            ui.label("Maintenance").classes("font-bold")
            with ui.row().classes("gap-2"):
                async def backup_now():
                    warning = await run.io_bound(backup.run_startup_backup)
                    ui.notify(warning or "Backup created ✓",
                              type="warning" if warning else "positive",
                              multi_line=True)

                async def poll_now():
                    ui.notify("Polling all active profiles…")
                    counters = await polling.poll_all_profiles(force=True)
                    ui.notify(f"Done: {counters['new']} new jobs", type="positive")

                ui.button("Backup now", icon="save", on_click=backup_now) \
                    .props("outline")
                ui.button("Poll all profiles now", icon="refresh", on_click=poll_now) \
                    .props("outline")
