"""Dashboard: application statistics, follow-up reminders, recent activity."""

from nicegui import run, ui

from jobdeck import db
from jobdeck.constants import BEANTWORTET_STATUS, OFFENE_STATUS, STATUS_OPTIONS
from jobdeck.dates import days_since, iso_to_de
from jobdeck.ui.layout import frame

FOLLOW_UP_DEFAULT = 14


def _load():
    with db.db() as con:
        apps = [dict(r) for r in db.list_bewerbungen(con)]
        jobs = db.count_jobs_by_status(con)
        activity = [dict(r) for r in db.recent_activity(con, limit=10)]
        threshold = int(db.get_setting(con, "follow_up_days", str(FOLLOW_UP_DEFAULT)))
    return apps, jobs, activity, threshold


@ui.page("/")
async def dashboard_page():
    with frame("Dashboard"):
        apps, jobs, activity, threshold = await run.io_bound(_load)

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
            and (days_since(a.get("gesendet_am") or "") or 0) >= threshold
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
