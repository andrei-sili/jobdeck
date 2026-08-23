"""Dashboard: application statistics, follow-up reminders, recent activity."""

from nicegui import run, ui

from jobdeck import constants, db
from jobdeck import settings as app_settings
from jobdeck.constants import BEANTWORTET_STATUS, OFFENE_STATUS, STATUS_OPTIONS
from jobdeck.dates import days_since, iso_to_de, silence_anchor
from jobdeck.ui import live
from jobdeck.ui.layout import frame

FOLLOW_UP_DEFAULT = constants.DEFAULT_FOLLOW_UP_DAYS


def _load():
    with db.db() as con:
        signature = db.data_signature(con)  # first: see jobs._load_jobs
        return {
            "apps": [dict(r) for r in db.list_bewerbungen(con)],
            "jobs": db.count_jobs_by_status(con),
            "activity": [dict(r) for r in db.recent_activity(con, limit=10)],
            "threshold": app_settings.integer(
                con,
                "follow_up_days",
                FOLLOW_UP_DEFAULT,
                minimum=1,
                clamp=False,
            ),
            "signature": signature,
        }


def _signature() -> tuple:
    with db.db() as con:
        return db.data_signature(con)


def _render(view: dict) -> None:
    apps, jobs = view["apps"], view["jobs"]
    activity, threshold = view["activity"], view["threshold"]

    total = len(apps)
    counts: dict[str, int] = {}
    for app_row in apps:
        status = app_row.get("status") or "—"
        counts[status] = counts.get(status, 0) + 1
    answered = sum(counts.get(s, 0) for s in BEANTWORTET_STATUS)
    quote = round(answered / total * 100) if total else 0

    with ui.row().classes("w-full gap-4"):
        for label, value in [
            ("Applications", total),
            ("Response rate", f"{quote}%"),
            ("New jobs found", jobs.get("new", 0)),
            *[(s, counts[s]) for s in STATUS_OPTIONS if counts.get(s)],
        ]:
            with ui.card().classes("items-center min-w-32"):
                ui.label(str(value)).classes("text-3xl font-bold text-primary")
                ui.label(label).classes("text-xs text-gray-500")

    due = [
        a for a in apps
        if (a.get("status") or "") in OFFENE_STATUS
        and (days_since(silence_anchor(a)) or 0) >= threshold
    ]
    with ui.card().classes("w-full"):
        ui.label(f"⏰ Follow-up due ({len(due)} open for more than {threshold} days)") \
            .classes("font-bold text-amber-700")
        if not due:
            ui.label("Nothing due — all caught up.").classes("text-gray-500")
        for a in due:
            ds = days_since(a.get("gesendet_am") or "")
            ui.label(
                f"{a['firma']} — sent {iso_to_de(a.get('gesendet_am') or '')}"
                f" ({ds} days ago) · {a.get('email') or 'no e-mail on file'}"
            )

    with ui.card().classes("w-full"):
        ui.label("Recent activity").classes("font-bold")
        if not activity:
            ui.label("No status changes yet.").classes("text-gray-500")
        for h in activity:
            arrow = f"{h['old_status'] or '—'} → {h['new_status']}"
            ui.label(f"{h['created_at'][:16]}  {h['firma']}: {arrow} ({h['source']})") \
                .classes("text-sm")


# Off the rail as of the redesign: its three cards are being absorbed by the
# rubrics that own them (the follow-up list by Bewerbungen, the counters by the
# rail itself). It keeps a route while that is still true of only some of them —
# deleting a screen before its replacement exists is how a working app loses a
# feature in the middle of a redesign.
@ui.page("/dashboard")
async def dashboard_page():
    async with frame("Dashboard"):
        # It was rendered once and never again: the follow-up list is computed
        # from "today", applications are recorded by the queue, the cockpit and
        # auto-send, and "New jobs found" moves on every poll. A home screen
        # that never moves is the strongest single reason the app read as dead.
        header = ui.row().classes("w-full items-center")
        body = ui.column().classes("w-full gap-4")

        async def refresh():
            view = await run.io_bound(_load)
            live_view.mark(view["signature"])
            body.clear()
            with body:
                _render(view)

        with header:
            live_view = live.watch(_signature, refresh,
                                   busy=live.dialog_open)
        await refresh()
