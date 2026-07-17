"""Settings: paths, credentials status, tunables, maintenance actions."""

import asyncio

from nicegui import run, ui

from jobdeck import backup, config, db, gmail
from jobdeck.services import polling, scoring
from jobdeck.ui.helpers import open_in_system
from jobdeck.ui.layout import frame


def _get_settings():
    with db.db() as con:
        return {
            "follow_up_days": db.get_setting(con, "follow_up_days", "14"),
            "daily_send_cap": db.get_setting(con, "daily_send_cap", "15"),
            "ai_enabled": db.ai_enabled(con),
            "applicant_name": db.get_setting(con, "applicant_name", ""),
            "applicant_ort": db.get_setting(con, "applicant_ort", ""),
            "template_path": db.get_setting(con, "template_path", ""),
            "anlagen_dir": db.get_setting(con, "anlagen_dir", ""),
            "real_send_enabled": db.get_setting(con, "real_send_enabled", "0"),
            "test_recipient": db.get_setting(con, "test_recipient", ""),
            "gmail_address": db.get_setting(con, "gmail_address", ""),
            "sent_today": db.count_outbound_today(con),
            "llm_calls": db.get_setting(con, "llm_calls", "0"),
            "llm_input_tokens": db.get_setting(con, "llm_input_tokens", "0"),
            "llm_output_tokens": db.get_setting(con, "llm_output_tokens", "0"),
            "llm_cost_usd": db.get_setting(con, "llm_cost_usd", "0"),
        }


def _set_setting(key, value):
    with db.db() as con:
        db.set_setting(con, key, value)


def _get_setting(key, default=""):
    with db.db() as con:
        return db.get_setting(con, key, default)


def _ai_enabled():
    with db.db() as con:
        return db.ai_enabled(con)


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
            gmail_label = ui.label(
                f"Gmail connected as {settings['gmail_address']}"
                if settings["gmail_address"] and config.TOKEN_PATH.exists()
                else ""
            ).classes("text-sm text-gray-600")

            async def connect_gmail():
                # Pre-check the common first-run case so the notification
                # cannot promise a consent window that never opens.
                if not config.CLIENT_SECRET_PATH.exists():
                    ui.notify(f"no OAuth client file at "
                              f"{config.CLIENT_SECRET_PATH} — create a "
                              f"Desktop-app OAuth client in Google Cloud and "
                              f"save its JSON there",
                              type="warning", multi_line=True)
                    return
                ui.notify("Complete the Google consent in the browser window "
                          "that just opened…")
                try:
                    address = await run.io_bound(gmail.connect)
                except gmail.GmailError as exc:
                    ui.notify(str(exc), type="warning", multi_line=True)
                    return
                await run.io_bound(_set_setting, "gmail_address", address)
                gmail_label.set_text(f"Gmail connected as {address}"
                                     if address else "Gmail connected")
                ui.notify("Gmail connected ✓", type="positive")

            async def disconnect_gmail():
                await run.io_bound(gmail.disconnect)
                await run.io_bound(_set_setting, "gmail_address", "")
                gmail_label.set_text("")
                ui.notify("Gmail disconnected — the authorization was revoked "
                          "at Google and removed locally", type="info")

            with ui.row().classes("items-center gap-2"):
                ui.button("Connect Gmail", icon="link",
                          on_click=connect_gmail).props("outline dense")
                ui.button("Disconnect", icon="link_off",
                          on_click=disconnect_gmail).props("outline dense")
            ui.label(
                f"Keys are read from {config.ENV_PATH} (see .env.example in the repo)."
            ).classes("text-xs text-gray-500")

        with ui.card().classes("w-full"):
            ui.label("Tunables").classes("font-bold")
            follow_up = ui.number("Follow-up reminder after (days)",
                                  value=int(settings["follow_up_days"]),
                                  min=1, max=365).classes("w-64")
            cap = ui.number("Daily send cap (all sends count, test included)",
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
            ui.label("Application").classes("font-bold")
            applicant_name = ui.input(
                "Applicant name (used in the Betreff and e-mail signature)",
                value=settings["applicant_name"],
            ).classes("w-96")
            applicant_ort = ui.input(
                "City for the letter head (Ort)",
                value=settings["applicant_ort"],
            ).classes("w-96")
            template_path = ui.input(
                "Letter template path (HTML file with {{TOKEN}} placeholders)",
                value=settings["template_path"],
            ).classes("w-full")
            anlagen_dir = ui.input(
                "Anlagen folder (PDFs merged into the Mappe)",
                value=settings["anlagen_dir"],
            ).classes("w-full")
            ui.label(
                "Anlagen are appended in filename order — prefix them "
                "01_, 02_, … to control the sequence."
            ).classes("text-xs text-gray-500")

            async def save_application():
                await run.io_bound(_set_setting, "applicant_name",
                                   applicant_name.value.strip())
                await run.io_bound(_set_setting, "applicant_ort",
                                   applicant_ort.value.strip())
                await run.io_bound(_set_setting, "template_path",
                                   template_path.value.strip())
                await run.io_bound(_set_setting, "anlagen_dir",
                                   anlagen_dir.value.strip())
                ui.notify("Saved", type="positive")

            ui.button("Save", on_click=save_application)

        with ui.card().classes("w-full"):
            ui.label("Sending").classes("font-bold")
            test_recipient = ui.input(
                "Test recipient (every send goes here while real sending is OFF)",
                value=settings["test_recipient"],
            ).classes("w-96")

            async def save_test_recipient():
                value = test_recipient.value.strip()
                if value and not gmail.is_plausible_address(value):
                    ui.notify("that does not look like an e-mail address",
                              type="warning")
                    return
                await run.io_bound(_set_setting, "test_recipient", value)
                ui.notify("Saved", type="positive")

            ui.button("Save", on_click=save_test_recipient)

            # Same discipline as the AI kill switch: serialized writes, and
            # the dangerous direction (OFF→ON) needs an explicit confirm.
            real_write_lock = asyncio.Lock()

            async def confirm_real() -> bool:
                with ui.dialog() as confirm, ui.card():
                    ui.label("Enable REAL sending?").classes("font-bold")
                    ui.label("E-mails will go to the actual companies, not "
                             "the test recipient.").classes("text-sm")
                    with ui.row().classes("justify-end gap-2 w-full"):
                        ui.button("Cancel",
                                  on_click=lambda: confirm.submit(False)) \
                            .props("flat")
                        ui.button("Enable real sending",
                                  on_click=lambda: confirm.submit(True)) \
                            .props("color=negative")
                confirm.open()
                return bool(await confirm)

            async def toggle_real(e):
                stored = await run.io_bound(_get_setting,
                                            "real_send_enabled", "0")
                target = "1" if e.value else "0"
                if target == stored:
                    return  # programmatic echo of a reverted switch
                if e.value and not await confirm_real():
                    real_switch.set_value(False)
                    return
                async with real_write_lock:
                    await run.io_bound(_set_setting, "real_send_enabled",
                                       target)
                ui.notify("REAL sending is ON — every send goes to the "
                          "company now" if e.value
                          else "Back to test mode — sends go to the test "
                          "recipient",
                          type="warning" if e.value else "positive")

            real_switch = ui.switch(
                "Enable REAL sending (e-mails go to companies)",
                value=settings["real_send_enabled"] == "1",
                on_change=toggle_real,
            )
            ui.label(
                f"{settings['sent_today']} sent today · daily cap: "
                f"{settings['daily_send_cap']} (Tunables). While real "
                f"sending is off, nothing can reach a company."
            ).classes("text-xs text-gray-500")

        with ui.card().classes("w-full"):
            ui.label("AI").classes("font-bold")
            # Serializes rapid switch flips: without it two io_bound writes
            # can commit out of order and leave the DB at ON while the
            # switch shows OFF — the one state the kill switch must not lie
            # about.
            toggle_write_lock = asyncio.Lock()

            async def toggle_ai(e):
                async with toggle_write_lock:
                    await run.io_bound(_set_setting, "ai_enabled",
                                       "1" if e.value else "0")
                ui.notify("AI enabled — new jobs will be match-scored"
                          if e.value else "AI disabled — no LLM spend",
                          type="positive" if e.value else "info")

            ui.switch("Enable AI features (match scoring)",
                      value=settings["ai_enabled"], on_change=toggle_ai)
            ui.label(
                "Master switch for all LLM spend. While off, nothing is sent "
                "to the API — scheduled and manual scoring both skip."
            ).classes("text-xs text-gray-500")
            ui.label(
                f"{settings['llm_calls']} calls · "
                f"{settings['llm_input_tokens']} in / "
                f"{settings['llm_output_tokens']} out tokens · "
                f"${float(settings['llm_cost_usd']):.4f}"
            ).classes("text-sm")
            ui.label(
                f"Model: {config.anthropic_model()} (set ANTHROPIC_MODEL to change). "
                f"Match scoring needs {config.PROFILE_PATH} — see profile.example.md."
            ).classes("text-xs text-gray-500")

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

                async def score_now():
                    if not await run.io_bound(_ai_enabled):
                        ui.notify("AI is disabled — flip the switch in the AI "
                                  "card first", type="warning")
                        return
                    ui.notify("Scoring new jobs…")
                    counters = await scoring.score_new_jobs()
                    ui.notify(
                        f"Done: {counters['scored']} scored, "
                        f"{counters['failed']} failed",
                        type="positive" if not counters["failed"] else "warning",
                    )

                ui.button("Backup now", icon="save", on_click=backup_now) \
                    .props("outline")
                ui.button("Poll all profiles now", icon="refresh", on_click=poll_now) \
                    .props("outline")
                ui.button("Score new jobs now", icon="grade", on_click=score_now) \
                    .props("outline")
