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
from jobdeck.dates import days_since, silence_anchor
from jobdeck.services import liveness
from jobdeck.services import unterlagen as unterlagen_service
from jobdeck.services.unterlagen import RAIL_PARTS
from jobdeck.ui import live

# Where each rubric goes.
UNTERLAGEN_PATH = "/unterlagen"
STELLEN_PATH = "/"
BEWERBUNGEN_PATH = "/bewerbungen"
ANTWORTEN_PATH = "/antworten"
EINSTELLUNGEN_PATH = "/settings"
# The second face of the Bewerbungen rubric, not a rubric of its own — see
# `shelf` below for why the queue never became one.
POSTAUSGANG_PATH = "/queue"

FOLLOW_UP_DEFAULT = constants.DEFAULT_FOLLOW_UP_DAYS
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


def clock(iso: str) -> str:
    """`_clock` for a screen that has no clock of its own to pass in.

    Shared rather than reimplemented: the Suchprofil panel used to print the
    raw ISO fragment ("zuletzt gesucht 2026-08-14T10:22") in the middle of
    German prose, on a screen whose credibility rests on its German.
    """
    return _clock(iso, datetime.datetime.now())


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
            # What the shelf opens, not what may be sent: the two differ by a
            # failed draft and a stuck send, and both of those need him.
            "in_progress": db.count_open_drafts(con),
            "started": db.count_started_forms(con),
            "apps": [dict(row) for row in db.list_bewerbungen(con)],
            "follow_up_days": _int_setting(
                db.get_setting(con, "follow_up_days", ""), FOLLOW_UP_DEFAULT),
            "sent_today": db.count_outbound_today(con),
            "send_cap": _int_setting(
                db.get_setting(con, "daily_send_cap", ""), SEND_CAP_DEFAULT),
            "unterlagen": unterlagen_service.rail_facts(con),
            "connections": connections(),
            "replies_pending": db.count_pending_replies(con),
            "replies_total": db.count_inbound_replies(con),
            "replies_recent_einladung": db.count_recent_invitations(con),
            "replies_last_poll": db.get_setting(con, "replies_last_poll_at", ""),
            "replies_last_error": db.get_setting(con, "replies_last_error", ""),
            "gmail_can_read": gmail.can_read(),
        }


# The settings and files the rail states but no table signature can see. It is
# the surface this redesign added to END silent staleness, so it must not be
# stale itself: connecting Gmail on the very page beside it used to leave it
# reading "Gmail fehlt" for the life of the page.
_WATCHED_SETTINGS = ("follow_up_days", "daily_send_cap", "stale_age_days",
                     "replies_last_poll_at", "replies_last_error")


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
        # The documents are files, not rows: no table signature can see one
        # arriving in the Anlagen folder, and the rubric above states how many
        # there are.
        unterlagen_service.rail_fingerprint(con),
        *(present for _name, present in connections()),
        # token PRESENCE is above; the read SCOPE is a different fact, and a
        # re-connect that adds it must reach the Antworten rubric
        gmail.can_read(),
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
        and (days_since(silence_anchor(a)) or 0) >= follow_up_days
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
        _unterlagen_rubric(view["unterlagen"]),
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
            # A form he has begun and not closed is the one thing here worth
            # seeing from Einstellungen: it is an application that may already
            # be out, and it is the only state the app cannot resolve by
            # itself. Absent entirely when there are none — a permanent "0
            # laufen" is a line you stop reading.
            sub=(f"{view['jobs_total']} Anzeigen · "
                 f"{view['working']} von {view['companies_total']} Firmen offen"
                 + (f" · {view['started']} laufen" if view["started"] else "")),
            fill=_share(view["working"], view["companies_total"]),
            amber=bool(view["started"]),
        ),
        Rubric(
            key="bewerbungen",
            label="Bewerbungen",
            path=BEWERBUNGEN_PATH,
            # "überfällig", not "ohne Antwort": the screen behind this uses
            # those three words for every open application, and this figure is
            # only the ones past the follow-up threshold. One click apart,
            # they were two different numbers under one wording.
            count=(f"{silent} überfällig" if silent
                   else f"{total} Bewerbungen"),
            # "gesendet" would be a claim about this app: the register holds
            # every application he has ever recorded, including the ones
            # imported from the old tracker.
            sub=f"{total} Bewerbungen · {answered} beantwortet",
            fill=_share(answered, total),
            amber=bool(silent),
        ),
        _antworten_rubric(view, now),
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


def _unterlagen_rubric(facts: dict) -> Rubric:
    """The documents rubric, about documents.

    It used to count SEARCH PROFILES — "3 Profile" under the heading
    Unterlagen, with the last poll time beneath it. Both facts are real and
    neither is about what an employer receives, on the one rubric he opened
    looking for his CV. The profiles say what they have to say in the Puls,
    where the engine reports itself.

    The sub-line names the FIRST thing standing between him and a Mappe that
    could be sent, in the order they block each other: without the template
    there is no letter to attach anything to, without an Anlage the Mappe is
    the letter alone, and without a build nothing on the screen has been
    measured.
    """
    documents = int(facts["documents"])
    anlagen = int(facts["anlagen"])
    present = sum((bool(facts["template_ok"]), bool(anlagen),
                   bool(facts["built"])))
    if not facts["template_ok"]:
        sub = "Vorlage fehlt"
    elif facts["folder_state"] == "unset":
        sub = "kein Ordner für Anlagen"
    elif facts["folder_state"] == "missing":
        sub = "Anlagen-Ordner fehlt"
    elif facts["folder_state"] == "unreadable":
        sub = "Anlagen-Ordner nicht lesbar"
    elif not anlagen:
        sub = "keine Anlagen — nur der Brief"
    elif not facts["built"]:
        sub = "Mappe noch nie gebaut"
    else:
        sub = f"Vorlage + {anlagen} Anlagen" if anlagen != 1 \
            else "Vorlage + 1 Anlage"
    return Rubric(
        key="unterlagen",
        label="Unterlagen",
        path=UNTERLAGEN_PATH,
        count=("1 Dokument" if documents == 1 else f"{documents} Dokumente"),
        sub=sub,
        fill=_share(present, RAIL_PARTS),
        amber=present < RAIL_PARTS,
    )


def _antworten_rubric(view: dict, now: datetime.datetime) -> Rubric:
    """The reply rubric, honest about its precondition.

    Reading needs the modify scope, which a pre-Phase-3 token does not
    carry — so the first thing this line can say is what a re-connect would
    add. Once reading runs, an unreviewed pile outranks the ledger count,
    and a fresh invitation outranks both: it is the one inbound event he
    would want tapped on the shoulder for."""
    pending = int(view["replies_pending"])
    total = int(view["replies_total"])
    invitations = int(view["replies_recent_einladung"])
    gmail_connected = dict(view["connections"]).get("Gmail", False)
    if invitations:
        count = ("1 Einladung!" if invitations == 1
                 else f"{invitations} Einladungen!")
    elif pending:
        count = f"{pending} zu prüfen"
    elif total == 1:
        count = "1 Antwort"
    else:
        count = f"{total} Antworten"
    if not gmail_connected:
        sub = "Gmail ist nicht verbunden"
    elif not view["gmail_can_read"]:
        sub = "Gmail ohne Lese-Zugriff — neu verbinden"
    elif view["replies_last_error"]:
        sub = "Gmail-Lesen gestört"
    elif view["replies_last_poll"]:
        sub = f"Gmail liest mit · zuletzt {_clock(view['replies_last_poll'], now)}"
    else:
        sub = "Gmail liest mit — erster Lauf steht aus"
    amber = bool(pending or invitations
                 or not gmail_connected or not view["gmail_can_read"]
                 or view["replies_last_error"])
    return Rubric(
        key="antworten",
        label="Antworten",
        path=ANTWORTEN_PATH,
        count=count,
        sub=sub,
        fill=_share(total, total + pending),
        amber=amber,
    )


def pulse(view: dict, now: datetime.datetime) -> list[Pulse]:
    """What the engine did last, in three lines.

    Derived from timestamps the work itself left behind rather than from the
    scheduler: a screen that asked the scheduler would say a job is "scheduled"
    while it silently fails, which is exactly the reassurance this must not
    give.
    """
    active, last_polled, errors = view["profiles"]
    last_checked, unchecked = view["liveness"]
    unscored = view["unscored"]
    return [
        # What the Unterlagen rubric used to say. It belongs here: a search
        # profile is not a document, and this is the line that reports whether
        # the engine is running. A profile whose source refused outranks the
        # clock — the clock would say the pass ran, which is true and beside
        # the point when it came back with nothing.
        Pulse("Suche",
              "kein aktives Profil" if not active else
              (f"{errors} Profil ohne Antwort" if errors == 1 else
               f"{errors} Profile ohne Antwort") if errors else
              _clock(last_polled, now) or "noch nie",
              "idle" if not active else _beat(last_polled, now)),
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


def shelf(view: dict) -> str:
    """"2 Briefe warten · 3 von 5 heute frei", or '' for no shelf at all.

    His decision (2026-08-14): the review queue gets no sixth rubric. A rubric
    found empty nine times out of ten teaches you to ignore it, and the queue
    is not somewhere you browse — it is a stack that drains to zero. So it
    appears in the foot only while something is in it, and disappears rather
    than standing there reading "0 warten".

    It carries the one figure that decides whether pressing Senden is even
    possible, which until now he only met inside the send screen itself.
    """
    waiting = view["in_progress"]
    if waiting <= 0:
        return ""
    used, cap = budget(view)
    letters = "1 Brief wartet" if waiting == 1 else f"{waiting} Briefe warten"
    # `budget` already clamps `used` to the cap, so the subtraction cannot go
    # negative — a second guard here was unreachable code that read like a
    # guarantee, and the test named after it could not fail.
    return f"{letters} · {cap - used} von {cap} heute frei"


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


def _render_foot(view: dict, now: datetime.datetime,
                 with_shelf: bool = True) -> None:
    used, cap = budget(view)
    line = shelf(view) if with_shelf else ""
    if line:
        # Above the budget rather than below it: it is the only thing in the
        # foot he can act on, and it is the reason the budget matters.
        element = ui.element("button").classes("jd-shelf") \
            .mark("postausgang-shelf")
        element.on("click", lambda _=None: ui.navigate.to(POSTAUSGANG_PATH))
        with element:
            ui.label("Postausgang").classes("jd-shelf-name")
            ui.label(line).classes("jd-shelf-sub")
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


async def install(current: str, with_shelf: bool = True) -> None:
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

    `with_shelf` is False on the Postausgang itself. Everything else in the
    foot reports; the shelf is the one element that means "there is something
    ELSEWHERE to do", and on that screen it was a button navigating to the
    page you were already on. It cannot be derived from `current`, because
    both faces of the rubric mark the same one.
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
            _render_foot(view, now, with_shelf)

    watcher = live.watch(signature, refresh)
    await refresh()
