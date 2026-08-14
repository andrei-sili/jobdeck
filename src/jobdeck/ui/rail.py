"""The rail: where the work sits, and proof the machine is running.

A navigation bar lists destinations; this one states how much is waiting behind
each of them and what the engine did last. That is the whole difference between
a menu and a spine, and it is the answer to the complaint that started the
redesign — the app moved on its own and never said so, so it read as dead.

Every number here is derived from the database, never stored: a counter a writer
has to remember to bump is a counter that goes stale, which is the defect class
the live watcher exists to end. The rubrics are computed as plain data first and
rendered second, so what the rail claims can be pinned without a browser.
"""

import datetime
from dataclasses import dataclass

from nicegui import run, ui

from jobdeck import config, constants, db, freshness, gmail
from jobdeck.constants import BEANTWORTET_STATUS, OFFENE_STATUS
from jobdeck.dates import days_since
from jobdeck.services import liveness
from jobdeck.ui import live

# Where each rubric goes. Two of them still open the screens they will absorb
# in their own slices — Unterlagen already means "the Suchprofil and what gets
# sent", and Bewerbungen already means the register. Pointing them at nothing
# until then would take away the only way he has to send anything.
UNTERLAGEN_PATH = "/unterlagen"
STELLEN_PATH = "/"
BEWERBUNGEN_PATH = "/applications"
EINSTELLUNGEN_PATH = "/settings"

FOLLOW_UP_DEFAULT = 14
SEND_CAP_DEFAULT = int(constants.DEFAULT_DAILY_CAP)

# How recently the poller must have run for the rail to call discovery live.
# The scheduler wakes every five minutes, so anything inside that window is a
# pass that has just happened or is happening now.
RUNNING_WITHIN_MIN = 5

# At most this many boxes are drawn for the daily send budget. A cap of 40 as a
# row of 40 squares is a smear, and the number beside it says the truth anyway.
BUDGET_BOXES = 10


@dataclass(frozen=True)
class Rubric:
    """One line of the spine: a name, what is waiting, and how full it is."""

    key: str
    label: str
    path: str
    count: str
    sub: str
    fill: float
    amber: bool = False
    enabled: bool = True


@dataclass(frozen=True)
class Pulse:
    """One line of the engine's heartbeat. `state` is 'run', 'ok' or 'idle'."""

    label: str
    detail: str
    state: str


def _int_setting(raw: str, default: int) -> int:
    """A stored setting as a whole number, falling back rather than raising.

    Settings are free text in a table he can edit, and every one of these is
    read while a page is being built: `int("")` taking down the whole rail is
    the same failure shape that once took down the inbox over a non-finite age
    threshold."""
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError, OverflowError):
        return default


def _clock(iso: str, now: datetime.datetime) -> str:
    """'14:38' for something that happened today, '11.08.' before that, ''
    for never — the rail has room for one of the two, never both.

    `now` is passed in rather than read here so what the rail says is a pure
    function of what it was given; a helper that asked the clock itself would
    make every test of it true only on the day it was written."""
    parsed = _parse(iso)
    if parsed is None:
        return ""
    if parsed.date() == now.date():
        return parsed.strftime("%H:%M")
    return parsed.strftime("%d.%m.")


def _parse(iso: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None


def _minutes_since(iso: str, now: datetime.datetime) -> float | None:
    moment = _parse(iso)
    if moment is None:
        return None
    return (now - moment).total_seconds() / 60.0


def facts() -> dict:
    """Everything the rail says, in one short read."""
    with db.db() as con:
        # First, before a single count: sqlite3 gives every SELECT its own
        # snapshot, so a poll committing between them would marry stale numbers
        # to a fresh signature and the watcher would record that as current.
        signature = _signature(con)
        stale_age = freshness.stale_age_setting(
            db.get_setting(con, "stale_age_days", ""))
        working = {"mismatches": "exclude", "gone": "exclude",
                   "applied": "exclude", "old": "exclude",
                   "stale_age_days": stale_age}
        return {
            "signature": signature,
            "profiles": db.poll_progress(con),
            "unscored": db.count_unscored_jobs(con),
            "last_scored": db.get_setting(con, "llm_last_call_at", ""),
            "liveness": db.liveness_progress(con, liveness.probeable_sources()),
            "jobs_total": db.count_jobs(con),
            "companies_total": db.count_job_groups(con, "new"),
            "working": db.count_job_groups(con, "new", **working),
            "unread": db.count_job_groups(con, "new", opened="exclude", **working),
            "bookmarked": db.count_bookmarked_jobs(con),
            "in_progress": db.count_waiting_drafts(con),
            "apps": [dict(row) for row in db.list_bewerbungen(con)],
            "follow_up_days": _int_setting(
                db.get_setting(con, "follow_up_days", ""), FOLLOW_UP_DEFAULT),
            "sent_today": db.count_outbound_today(con),
            "send_cap": _int_setting(
                db.get_setting(con, "daily_send_cap", ""), SEND_CAP_DEFAULT),
            "connections": connections(),
        }


# The settings and files the rail states but no table signature can see. It is
# the surface this redesign added to END silent staleness, so it must not be
# stale itself: connecting Gmail on the very page beside it used to leave it
# reading "Gmail fehlt" for the life of the page.
_WATCHED_SETTINGS = ("follow_up_days", "daily_send_cap", "stale_age_days")


def _signature(con) -> tuple:
    """What the rail shows, compressed. The three shared signatures cover the
    pipeline, the poller and the send meter; the rest are read here because
    nothing else looks at them."""
    return (
        *db.data_signature(con),
        *db.profiles_signature(con),
        *db.meter_signature(con),
        # profiles_signature cannot see a profile being switched off, and
        # `poll_progress` counts only the active ones.
        db.count_active_profiles(con),
        *(db.get_setting(con, key, "") for key in _WATCHED_SETTINGS),
        *(present for _name, present in connections()),
    )


def signature() -> tuple:
    with db.db() as con:
        return _signature(con)


def connections() -> list[tuple[str, bool]]:
    """(name, present) for everything the app needs from outside itself.

    Presence only — a key is never read, printed or logged anywhere in this
    app, and a screen that says "connected" without ever holding the value is
    the whole point of asking config rather than the file."""
    return [
        ("Anthropic", bool(config.anthropic_api_key())),
        ("Gmail", gmail.is_connected()),
        ("Jooble", bool(config.jooble_api_key())),
    ]


def _application_counts(apps: list[dict], follow_up_days: int) -> tuple[int, int, int]:
    """(sent, answered, silent) over the whole register.

    'Silent' is the one that earns a place in the rail: an application still
    open past the follow-up threshold is the only thing in this app that needs
    him to do something about a company rather than about a posting."""
    answered = sum(1 for a in apps if (a.get("status") or "") in BEANTWORTET_STATUS)
    silent = sum(
        1 for a in apps
        if (a.get("status") or "") in OFFENE_STATUS
        and (days_since(a.get("gesendet_am") or "") or 0) >= follow_up_days
    )
    return len(apps), answered, silent


def _share(part: int, whole: int) -> float:
    return min(1.0, part / whole) if whole > 0 else 0.0


def rubrics(view: dict, current: str, now: datetime.datetime) -> list[Rubric]:
    """The five lines of the spine, as data. Pure: given the same numbers it
    says the same thing, which is what makes the claims testable."""
    active, last_polled, poll_errors = view["profiles"]
    total, answered, silent = _application_counts(
        view["apps"], view["follow_up_days"])
    connected = [name for name, ok in view["connections"] if ok]
    missing = [name for name, ok in view["connections"] if not ok]
    return [
        Rubric(
            key="unterlagen",
            label="Unterlagen",
            path=UNTERLAGEN_PATH,
            count=f"{active} Profil" if active == 1 else f"{active} Profile",
            sub=(f"{poll_errors} Profil ohne Antwort" if poll_errors == 1 else
                 f"{poll_errors} Profile ohne Antwort" if poll_errors else
                 f"zuletzt gesucht {_clock(last_polled, now)}" if last_polled else
                 "noch nie gesucht"),
            fill=1.0 if active else 0.0,
            amber=bool(poll_errors) or not active,
        ),
        Rubric(
            key="stellen",
            label="Stellen",
            path=STELLEN_PATH,
            count=f"{view['unread']} neu",
            # Both units named, and the bar divides LIKE BY LIKE. Printing
            # "803 gefunden · 166 in Arbeit" invited the reading that 637 were
            # filtered out, when most of the difference is simply postings
            # collapsing into companies — and the bar was companies ÷ postings,
            # so it would have read about 25 % with every pile empty.
            sub=f"{view['jobs_total']} Anzeigen · "
                f"{view['working']} von {view['companies_total']} Firmen offen",
            fill=_share(view["working"], view["companies_total"]),
        ),
        Rubric(
            key="bewerbungen",
            label="Bewerbungen",
            path=BEWERBUNGEN_PATH,
            count=(f"{silent} ohne Antwort" if silent
                   else f"{total} Bewerbungen"),
            # "gesendet" would be a claim about this app: the register holds
            # every application he has ever recorded, including the ones
            # imported from the old tracker.
            sub=f"{total} Bewerbungen · {answered} beantwortet",
            fill=_share(answered, total),
            amber=bool(silent),
        ),
        Rubric(
            key="antworten",
            label="Antworten",
            path="",
            count="bald",
            sub="Gmail liest mit — Phase 3",
            fill=0.0,
            enabled=False,
        ),
        Rubric(
            key="einstellungen",
            label="Einstellungen",
            path=EINSTELLUNGEN_PATH,
            count=f"{len(connected)}/{len(view['connections'])}",
            sub=(f"{missing[0]} fehlt" if len(missing) == 1 else
                 f"{len(missing)} Verbindungen fehlen" if missing else
                 "alle Verbindungen stehen"),
            fill=_share(len(connected), len(view["connections"])),
            amber=bool(missing),
        ),
    ]


def pulse(view: dict, now: datetime.datetime) -> list[Pulse]:
    """What the engine did last, in three lines.

    Derived from timestamps the work itself left behind rather than from the
    scheduler: a screen that asked the scheduler would say a job is "scheduled"
    while it silently fails, which is exactly the reassurance this must not
    give.
    """
    _active, last_polled, _errors = view["profiles"]
    last_checked, unchecked = view["liveness"]
    unscored = view["unscored"]
    return [
        Pulse("Suche", _clock(last_polled, now) or "noch nie",
              _beat(last_polled, now)),
        # The animated dot means "something ran just now". A BACKLOG is not
        # evidence of a worker: with AI spend switched off — his own default —
        # a queue of twelve pulsed forever while nothing was ever going to
        # score them.
        Pulse("Bewertung",
              f"{unscored} offen" if unscored else "alles bewertet",
              _beat(view["last_scored"], now)),
        Pulse("Anzeigen-Prüfung",
              f"{unchecked} offen" if unchecked
              else _clock(last_checked, now) or "nie",
              _beat(last_checked, now)),
    ]


def _beat(iso: str, now: datetime.datetime) -> str:
    """'run' while it is happening, 'ok' once it has, 'idle' if it never has."""
    since = _minutes_since(iso, now)
    if since is None:
        return "idle"
    return "run" if since <= RUNNING_WITHIN_MIN else "ok"


def budget(view: dict) -> tuple[int, int]:
    """(used, cap) for today's sending, both clamped to something drawable."""
    cap = max(0, view["send_cap"])
    return min(view["sent_today"], cap), cap


def _render_rubric(rubric: Rubric, current: str) -> None:
    element = ui.element("button").classes("jd-sec").props(
        f'data-current={"true" if rubric.key == current else "false"} '
        f'data-enabled={"true" if rubric.enabled else "false"}'
    )
    if rubric.enabled:
        path = rubric.path
        element.on("click", lambda _=None, p=path: ui.navigate.to(p))
    with element:
        with ui.row().classes("w-full items-baseline gap-2 no-wrap"):
            ui.label(rubric.label).classes("jd-sec-name")
            ui.label(rubric.count).classes(
                "jd-sec-count ml-auto" + (" amber" if rubric.amber else ""))
        with ui.element("span").classes("jd-track w-full mt-1"):
            ui.element("i").classes("amber" if rubric.amber else "") \
                .style(f"width:{round(rubric.fill * 100)}%")
        ui.label(rubric.sub).classes("jd-sec-sub block mt-1")


def _render(view: dict, current: str, now: datetime.datetime) -> None:
    for rubric in rubrics(view, current, now):
        _render_rubric(rubric, current)


def _render_foot(view: dict, now: datetime.datetime) -> None:
    used, cap = budget(view)
    ui.label("Heute gesendet").classes("jd-flabel")
    with ui.row().classes("items-center gap-1 mb-3"):
        for index in range(min(cap, BUDGET_BOXES)):
            ui.element("i").classes(
                "jd-budget-box" + (" on" if index < used else ""))
        ui.label(f"{used} von {cap}").classes("jd-pulse ml-1")
    ui.label("Puls").classes("jd-flabel")
    with ui.column().classes("gap-1 w-full"):
        for beat in pulse(view, now):
            with ui.row().classes("items-center gap-2 w-full no-wrap"):
                ui.element("i").classes(f"jd-pulse-dot {beat.state}")
                ui.label(beat.label).classes("jd-pulse")
                ui.label(beat.detail).classes("jd-pulse ml-auto")


async def install(current: str) -> None:
    """Build the rail into the current slot and keep it true while he works.

    Awaited as part of building the page rather than handed to a timer: a
    short one-shot timer created while the page function is still running gets
    its first tick in between that function's own awaits, and the page build
    came back from `run.io_bound` with None — NiceGUI's shape for "you were
    cancelled" — leaving a screen that was nothing but this rail. Loading the
    rail is part of opening a page, so it is awaited like one.

    Never defers: the rail holds no text he is reading and no row he could be
    acting on, so there is nothing for fresh numbers to yank out from under
    him — the chip the watcher owns therefore never appears.
    """
    ui.label("JobDeck").classes("jd-brand px-2 pt-1")
    spine = ui.column().classes("w-full gap-0 px-1 pt-2")
    foot = ui.column().classes("w-full gap-0 px-3 pb-3 pt-4 mt-auto")

    async def refresh() -> None:
        view = await run.io_bound(facts)
        if view is None:
            return  # the page is going away; there is nothing left to draw on
        watcher.mark(view["signature"])
        now = datetime.datetime.now()
        spine.clear()
        foot.clear()
        with spine:
            _render(view, current, now)
        with foot:
            _render_foot(view, now)

    watcher = live.watch(signature, refresh)
    await refresh()
