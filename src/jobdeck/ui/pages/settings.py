"""Settings: paths, credentials status, tunables, maintenance actions."""

import asyncio

from nicegui import run, ui

from jobdeck import apply_form, backup, config, db, freshness, gmail
from jobdeck.constants import DEFAULT_SILENCE_CLOSES_DAYS
from jobdeck.services import (
    apply_resolve,
    liveness,
    mappe,
    preparing,
    scoring,
    silence,
)
from jobdeck.services import replies as replies_service
from jobdeck.ui import live
from jobdeck.ui.helpers import open_in_system
from jobdeck.ui.layout import frame


def _get_settings():
    with db.db() as con:
        return {
            "follow_up_days": db.get_setting(con, "follow_up_days", "14"),
            "stale_age_days": freshness.stale_age_setting(
                db.get_setting(con, "stale_age_days", "")),
            "daily_send_cap": db.get_setting(con, "daily_send_cap", "15"),
            # Parsed by the rule's own parser, not a bare int(): a
            # hand-edited or non-numeric value must show the window that will
            # actually be used instead of raising out of the page that is the
            # only place to fix it.
            "silence_closes_after_days": silence.configured_days(con),
            "daily_draft_cap": db.daily_draft_cap(con),
            "drafts_today": db.count_drafts_today(con),
            "ai_enabled": db.ai_enabled(con),
            "web_contact_search": db.get_setting(con, "web_contact_search", "0"),
            "reply_ai_classify": db.get_setting(con, "reply_ai_classify", "0"),
            "applicant_name": db.get_setting(con, "applicant_name", ""),
            "applicant_ort": db.get_setting(con, "applicant_ort", ""),
            # the apply-cockpit fields, one entry per APPLICANT_LABELS key
            **{key: db.get_setting(con, key, "")
               for key in apply_form.APPLICANT_SETTINGS},
            "template_path": db.get_setting(con, "template_path", ""),
            "anlagen_dir": db.get_setting(con, "anlagen_dir", ""),
            "email_signature": db.get_setting(con, "email_signature", ""),
            "mappe_compress": db.get_setting(con, "mappe_compress", "1"),
            # Parsed the same way the builder parses it, so a hand-edited or
            # unset value shows the budget that will actually be used instead
            # of breaking the page on float("").
            "mappe_target_mb": mappe.target_mb_setting(
                db.get_setting(con, "mappe_target_mb", ""),
                mappe.DEFAULT_TARGET_MB),
            "mappe_target_portal_mb": mappe.target_mb_setting(
                db.get_setting(con, "mappe_target_portal_mb", ""),
                mappe.DEFAULT_PORTAL_TARGET_MB),
            "global_hard_tags": db.get_setting(con, "global_hard_tags", ""),
            "real_send_enabled": db.get_setting(con, "real_send_enabled", "0"),
            "test_recipient": db.get_setting(con, "test_recipient", ""),
            "gmail_address": db.get_setting(con, "gmail_address", ""),
            "sent_today": db.count_outbound_today(con),
            "llm_calls": db.get_setting(con, "llm_calls", "0"),
            "llm_input_tokens": db.get_setting(con, "llm_input_tokens", "0"),
            "llm_output_tokens": db.get_setting(con, "llm_output_tokens", "0"),
            "llm_cost_usd": db.get_setting(con, "llm_cost_usd", "0"),
        }


def _get_meters():
    """The numbers a settings page must not show stale: LLM spend and the
    sends counted against today's cap."""
    with db.db() as con:
        signature = db.meter_signature(con)  # first: see jobs._load_jobs
        return {
            "llm_calls": db.get_setting(con, "llm_calls", "0"),
            "llm_input_tokens": db.get_setting(con, "llm_input_tokens", "0"),
            "llm_output_tokens": db.get_setting(con, "llm_output_tokens", "0"),
            "llm_cost_usd": db.get_setting(con, "llm_cost_usd", "0"),
            "sent_today": db.count_outbound_today(con),
            "signature": signature,
        }


def _signature() -> tuple:
    with db.db() as con:
        return db.meter_signature(con)


def _prepare_filter():
    """The prepare-batch filter, defaults applied (see preparing.py)."""
    with db.db() as con:
        return preparing.settings(con)


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
    async with frame("Einstellungen", current="einstellungen"):
        settings = await run.io_bound(_get_settings)
        prep = await run.io_bound(_prepare_filter)
        live_host = ui.row().classes("w-full items-center")

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

            def _scope_line() -> str:
                """Sending and reading are two grants: a pre-Phase-3 token
                sends but cannot read replies, and only a re-connect (one
                consent) adds the read scope — installed apps cannot widen
                an existing token."""
                if not config.TOKEN_PATH.exists():
                    return ""
                if gmail.can_read():
                    return "Senden ✓ · Antworten lesen ✓"
                return ("Senden ✓ · Antworten lesen FEHLT — einmal "
                        "„Connect Gmail“ erteilt den Lese-Zugriff")

            scope_label = ui.label(
                await run.io_bound(_scope_line)).classes(
                    "text-sm text-gray-600")

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
                scope_label.set_text(await run.io_bound(_scope_line))
                ui.notify("Gmail connected ✓", type="positive")

            async def disconnect_gmail():
                await run.io_bound(gmail.disconnect)
                await run.io_bound(_set_setting, "gmail_address", "")
                gmail_label.set_text("")
                scope_label.set_text("")
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
            draft_cap = ui.number(
                "Tageslimit für Anschreiben (jede Formular-Bewerbung schreibt "
                "eines)",
                value=settings["daily_draft_cap"], min=0, max=100) \
                .classes("w-64")
            ui.label(
                f"Heute geschrieben: {settings['drafts_today']} von "
                f"{settings['daily_draft_cap']}. Ein Anschreiben kostet etwa "
                f"0,09 $. Das Limit gilt hart — die Bewerbung wird abgelehnt, "
                f"bis du es hier anhebst."
            ).classes("text-xs text-gray-500")
            stale_age = ui.number(
                "Anzeigen gelten als alt nach (Tagen)",
                value=settings["stale_age_days"], min=1, max=365).classes("w-64")
            ui.label(
                "Older postings leave the working list for the „Alte Anzeigen“ "
                "pile — counted under the list, one click away, never deleted."
            ).classes("text-xs text-gray-500")
            silence_days = ui.number(
                "Bewerbung ohne Antwort schließen nach (Tagen)",
                value=settings["silence_closes_after_days"],
                min=0, max=365).classes("w-64")
            ui.label(
                "Danach trägt JobDeck „Keine Antwort“ ein — kein „Absage“, "
                "denn abgesagt hat niemand: die Antwortquote zählt sie nicht "
                "mit. Meldet sich die Firma doch noch, überschreibt ihre "
                "Antwort den Eintrag von selbst. 0 schaltet die Regel ab."
            ).classes("text-xs text-gray-500")

            async def save():
                await run.io_bound(_set_setting, "follow_up_days",
                                   str(int(follow_up.value or 14)))
                await run.io_bound(_set_setting, "daily_send_cap",
                                   str(int(cap.value or 15)))
                await run.io_bound(
                    _set_setting, "daily_draft_cap",
                    str(max(0, int(draft_cap.value
                                   if draft_cap.value is not None else 10))))
                await run.io_bound(
                    _set_setting, "stale_age_days",
                    str(freshness.stale_age_setting(stale_age.value)))
                await run.io_bound(
                    _set_setting, silence.SETTING_DAYS,
                    str(max(0, int(silence_days.value
                                   if silence_days.value is not None
                                   else DEFAULT_SILENCE_CLOSES_DAYS))))
                ui.notify("Saved", type="positive")

            ui.button("Save", on_click=save).mark("save-tunables")

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
            compress = ui.switch(
                "Shrink the Mappe to fit the application channel",
                value=settings["mappe_compress"] == "1",
            )
            with ui.row().classes("items-center gap-4"):
                target_mb = ui.number("E-mail target (MB)",
                                      value=settings["mappe_target_mb"],
                                      min=0.5, max=20, step=0.5).classes("w-48")
                portal_mb = ui.number(
                    "Portal upload target (MB)",
                    value=settings["mappe_target_portal_mb"],
                    min=0.5, max=20, step=0.5,
                ).classes("w-48")
            ui.label(
                "Your Anlagen are never modified — only the merged copy that "
                "gets attached. Scans are downsampled no further than 200 dpi; "
                "if a target needs more than that, the Mappe stays larger and "
                "says so rather than going out unreadable."
            ).classes("text-xs text-gray-500")
            email_signature = ui.textarea(
                "E-mail signature (contact block under every application e-mail)",
                value=settings["email_signature"],
            ).classes("w-full").props("autogrow")
            ui.label(
                "Added by the app, never written by the AI — a model that "
                "mistypes one character of a URL or phone number costs a "
                "reply. Plain text only: bare URLs read as credibility links "
                "to spam filters, hidden links do not."
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
                await run.io_bound(_set_setting, "email_signature",
                                   (email_signature.value or "").strip())
                await run.io_bound(_set_setting, "mappe_compress",
                                   "1" if compress.value else "0")
                await run.io_bound(
                    _set_setting, "mappe_target_mb",
                    str(target_mb.value or mappe.DEFAULT_TARGET_MB))
                await run.io_bound(
                    _set_setting, "mappe_target_portal_mb",
                    str(portal_mb.value or mappe.DEFAULT_PORTAL_TARGET_MB))
                ui.notify("Saved", type="positive")

            ui.button("Save", on_click=save_application)

        with ui.card().classes("w-full"):
            ui.label("Bewerbungen vorbereiten").classes("font-bold")
            ui.label(
                "Picks the best-ranked postings that pass the filter, writes the "
                "Anschreiben and builds the Mappe for each, and tops the review "
                "queue up to the daily number. It only PREPARES — every send "
                "stays a separate click, and the send cap is untouched."
            ).classes("text-xs text-gray-500")
            with ui.row().classes("items-center gap-4 flex-wrap"):
                max_age = ui.number(
                    "Nicht älter als (Tage)", value=prep["max_age_days"],
                    min=1, max=365).classes("w-44")
                min_score = ui.number(
                    "Mindest-Score", value=prep["min_score"],
                    min=1, max=100).classes("w-40")
                per_day = ui.number(
                    "Pro Tag vorbereiten", value=prep["per_day"],
                    min=1, max=20).classes("w-44")
            include_forms = ui.switch(
                "Also postings that need a form (not just direct e-mail)",
                value=prep["include_forms"],
            ).tooltip("Nothing fresh carries a direct e-mail — with this off the "
                      "queue rarely fills")
            plan_label = ui.label().classes("text-sm")

            async def save_prepare_filter():
                for key, field, default in (
                    ("prepare_max_age_days", max_age, preparing.DEFAULT_MAX_AGE_DAYS),
                    ("prepare_min_score", min_score, preparing.DEFAULT_MIN_SCORE),
                    ("prepare_per_day", per_day, preparing.DEFAULT_PER_DAY),
                ):
                    await run.io_bound(_set_setting, key,
                                       str(int(field.value or default)))
                await run.io_bound(_set_setting, "prepare_include_forms",
                                   "1" if include_forms.value else "0")
                ui.notify("Saved", type="positive")
                await show_plan()

            async def show_plan():
                view = await run.io_bound(preparing.plan)
                if not view["candidates"]:
                    plan_label.set_text(
                        f"Nothing to prepare: {view['waiting']} applications are "
                        f"already waiting (target {view['config']['per_day']})."
                        if view["room"] == 0 else
                        "No posting passes the filter — widen the age or lower "
                        "the score."
                    )
                    return
                names = ", ".join(j["company"][:22] for j in view["candidates"])
                plan_label.set_text(
                    f"Would prepare {len(view['candidates'])} "
                    f"(≈ ${view['estimate_usd']:.2f}): {names}"
                )

            async def prepare_now():
                await show_plan()
                ui.notify("Writing the applications — about a minute each…")
                res = await preparing.prepare_day()
                if res.get("error"):
                    ui.notify(res["error"], type="warning", multi_line=True)
                    return
                if res.get("skipped"):
                    ui.notify("A run is already in progress", type="warning")
                    return
                if not res["candidates"]:
                    ui.notify(
                        f"Nothing to prepare — {res['waiting']} already waiting",
                        type="positive")
                    return
                tail = (f", {res['no_mappe']} without a Mappe"
                        if res["no_mappe"] else "")
                ui.notify(
                    f"{res['prepared']} prepared{tail}, {res['failed']} failed "
                    f"— ${res['cost_usd']:.2f}. Open the Postausgang.",
                    type="positive" if not res["failed"] else "warning",
                    multi_line=True)
                for problem in res["errors"][:3]:
                    ui.notify(problem, type="warning", multi_line=True)
                await show_plan()

            with ui.row().classes("items-center gap-2"):
                ui.button("Save filter", on_click=save_prepare_filter).props("outline")
                ui.button("Vorbereiten", icon="playlist_add",
                          on_click=prepare_now).props("color=primary")
                ui.button("Was würde vorbereitet?", icon="visibility",
                          on_click=show_plan).props("flat")

        with ui.card().classes("w-full"):
            ui.label("Bewerbungsdaten (für Formulare)").classes("font-bold")
            ui.label(
                "What portal and ATS forms ask for, every time. Filled in once "
                "here, one click away in the apply cockpit — the app never "
                "types into anyone else's form, it only stops you retyping "
                "your own address. Your name comes from the card above."
            ).classes("text-xs text-gray-500")
            form_inputs = {}
            with ui.row().classes("w-full gap-4 flex-wrap"):
                for key, label in apply_form.APPLICANT_LABELS.items():
                    if key == "applicant_name":
                        continue  # owned by the Application card, one input only
                    form_inputs[key] = ui.input(
                        label, value=settings.get(key, "")
                    ).classes("w-72")

            async def save_apply_profile():
                for key, field in form_inputs.items():
                    await run.io_bound(_set_setting, key,
                                       (field.value or "").strip())
                ui.notify("Saved", type="positive")

            ui.button("Save", on_click=save_apply_profile)

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
            sent_today_label = ui.label().classes("text-xs text-gray-500")

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

            async def toggle_web_search(e):
                async with toggle_write_lock:
                    await run.io_bound(_set_setting, "web_contact_search",
                                       "1" if e.value else "0")
                ui.notify("Web contact search on — an LLM may look up the "
                          "employer domain" if e.value
                          else "Web contact search off",
                          type="positive" if e.value else "info")

            ui.switch("Find the application e-mail via web search (AI)",
                      value=settings["web_contact_search"] == "1",
                      on_change=toggle_web_search)
            ui.label(
                "On demand only: when a posting has no e-mail, an LLM web search "
                "finds the employer domain, then its Impressum is checked for a "
                "bewerbung@ address (domain-verified). A few cents per lookup; "
                "off by default."
            ).classes("text-xs text-gray-500")

            async def toggle_reply_ai(e):
                async with toggle_write_lock:
                    await run.io_bound(_set_setting, "reply_ai_classify",
                                       "1" if e.value else "0")
                ui.notify("Reply classification by AI on — ambiguous replies "
                          "get a proposed category" if e.value
                          else "Reply classification by AI off",
                          type="positive" if e.value else "info")

            ui.switch("Classify ambiguous replies with AI",
                      value=settings["reply_ai_classify"] == "1",
                      on_change=toggle_reply_ai)
            ui.label(
                "Only for replies the German keyword rules cannot place, and "
                "only as a PROPOSAL on the Antworten page — the AI never "
                "changes a status by itself. Reply text is sent to the API "
                "solely for this classification. Fractions of a cent per "
                "mail; off by default."
            ).classes("text-xs text-gray-500")
            global_tags = ui.textarea(
                "Hard requirements for EVERY search (one per line)",
                value=settings["global_hard_tags"],
            ).classes("w-full").props("autogrow")
            ui.label(
                "A posting that clearly violates one scores 0 and is hidden "
                "behind the inbox's mismatch toggle — never deleted. Each "
                "search profile can add its own on top. Rules written in "
                "profile.md are NOT enforced here: that file states facts "
                "about you, this states what a posting must satisfy."
            ).classes("text-xs text-gray-500")

            async def save_global_tags():
                await run.io_bound(_set_setting, "global_hard_tags",
                                   (global_tags.value or "").strip())
                ui.notify("Saved — applies to the next scoring run",
                          type="positive")

            ui.button("Save", on_click=save_global_tags).props("outline")
            meter_label = ui.label().classes("text-sm")
            ui.label(
                f"Model: {config.anthropic_model()} (set ANTHROPIC_MODEL to change). "
                f"Match scoring needs {config.PROFILE_PATH} — see profile.example.md."
            ).classes("text-xs text-gray-500")

        def show_meters(meters: dict) -> None:
            """The two numbers that move while this page sits open: what the
            background scoring has spent (a product principle — the cost is
            shown in the UI) and how much of today's send cap is used.

            Only these. The rest of the page is INPUTS, and re-rendering an
            input he is typing in would throw his text away, which is why the
            page does not rebuild itself as a whole."""
            meter_label.set_text(
                f"{meters['llm_calls']} calls · "
                f"{meters['llm_input_tokens']} in / "
                f"{meters['llm_output_tokens']} out tokens · "
                f"${float(meters['llm_cost_usd']):.4f}"
            )
            sent_today_label.set_text(
                f"{meters['sent_today']} sent today · daily cap: "
                f"{settings['daily_send_cap']} (Tunables). While real "
                f"sending is off, nothing can reach a company."
            )

        async def refresh_meters():
            meters = await run.io_bound(_get_meters)
            live_view.mark(meters["signature"])
            show_meters(meters)

        with live_host:
            live_view = live.watch(_signature, refresh_meters,
                                   busy=live.dialog_open)
        show_meters({**settings, "signature": None})

        with ui.card().classes("w-full"):
            ui.label("Maintenance").classes("font-bold")
            with ui.row().classes("gap-2"):
                async def backup_now():
                    warning = await run.io_bound(backup.run_startup_backup)
                    ui.notify(warning or "Backup created ✓",
                              type="warning" if warning else "positive",
                              multi_line=True)

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

                async def resolve_channels_now():
                    ui.notify("Resolving where to apply…")
                    r = await apply_resolve.resolve_pending()
                    if r.get("skipped"):
                        # The scheduler runs the same pass every half hour, so
                        # this is a normal answer now — and it looks exactly
                        # like "nothing left to do" in the counters.
                        ui.notify(f"Ein Durchlauf läuft gerade — "
                                  f"{r['remaining']} noch offen.",
                                  type="warning")
                        return
                    if not r["resolved"] and not r["failed"]:
                        ui.notify("Every scored posting already knows its "
                                  "channel ✓", type="positive")
                        return
                    breakdown = " · ".join(
                        f"{n}× {c}" for c, n in sorted(
                            r["channels"].items(), key=lambda kv: -kv[1]))
                    tail = (f" — {r['remaining']} still pending, click again"
                            if r["remaining"] else "")
                    ui.notify(f"Resolved {r['resolved']}{tail}\n{breakdown}",
                              type="positive", multi_line=True)

                async def check_liveness_now():
                    ui.notify("Checking whether the ads are still online…")
                    r = await liveness.check_pending()
                    if not r["checked"]:
                        ui.notify("Every posting was checked within the last "
                                  "day ✓", type="positive")
                        return
                    ui.notify(
                        f"Checked {r['checked']}: {r['gone']} gone, "
                        f"{r['alive']} still online, {r['unknown']} no answer",
                        type="warning" if r["gone"] else "positive",
                        multi_line=True,
                    )

                async def ingest_replies_now():
                    ui.notify("Reading the inbox for replies…")
                    r = await replies_service.ingest_replies()
                    if r.get("skipped"):
                        ui.notify("A pass is already running.", type="warning")
                        return
                    if r.get("error"):
                        ui.notify(str(r["error"]), type="warning",
                                  multi_line=True)
                        return
                    ui.notify(
                        f"Read {r['seen']}: {r['auto_status']} filed, "
                        f"{r['receipts']} receipts, {r['review']} to review",
                        type="positive",
                    )

                ui.button("Backup now", icon="save", on_click=backup_now) \
                    .props("outline")
                ui.button("Resolve apply channels", icon="alt_route",
                          on_click=resolve_channels_now).props("outline")
                ui.button("Check postings still online", icon="wifi_tethering",
                          on_click=check_liveness_now).props("outline")
                ui.button("Score new jobs now", icon="grade", on_click=score_now) \
                    .props("outline")
                ui.button("Read replies now", icon="mark_email_read",
                          on_click=ingest_replies_now).props("outline")
