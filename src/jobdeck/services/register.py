"""What the Bewerbungen screen states, computed before anything is drawn.

Pure functions over rows, in the shape `ui/rail.py` established: the numbers
are derived here and rendered second, so every claim the screen makes can be
pinned without a browser — and so a claim and its bar cannot disagree.

Two honesty rules govern this module, both forced by his real register:

* **The pipeline does not nest.** 45 postings have a letter and only 33 were
  ever opened, because the daily batch and the form flow write one without him
  reading the ad first. Drawn as a funnel of subsets that is simply false, so
  the step that is not a subset says so in its own note rather than in a
  caption somewhere else.
* **The register is older than the app.** 44 of his 76 applications were
  imported from the tracker he kept before JobDeck existed. A screen that
  called all 76 "gesendet" would be claiming credit for them, so what this app
  did and what the register holds are two separate blocks.
"""

import datetime
from dataclasses import dataclass

from jobdeck import db
from jobdeck.constants import BEANTWORTET_STATUS, OFFENE_STATUS
from jobdeck.dates import MONATE_DE

# How far back the rhythm strip reaches. Sixty days is what makes a pause
# legible as a pause: his own register holds a 37-day gap between two bursts,
# and at 30 days that gap is the whole picture rather than a feature of it.
RHYTHM_DAYS = 60

# Applications below this many, and a percentage is arithmetic rather than
# evidence. The screen still shows the counts — it just refuses to lead with a
# rate the data cannot carry.
ENOUGH_FOR_A_RATE = 20


@dataclass(frozen=True)
class Step:
    """One measured population on the way from an ad to an application."""

    key: str
    label: str
    count: int
    share: float          # of the first step, for the bar
    note: str = ""        # what this number does NOT mean


@dataclass(frozen=True)
class Waiting:
    """An application that has had no answer, and for how long."""

    bewerbung_id: int
    firma: str
    days: int | None      # None when the row states no date at all
    overdue: bool
    kanal: str


@dataclass(frozen=True)
class Share:
    """A labelled part of a whole — one bar of a comparison."""

    label: str
    part: int
    whole: int
    ratio: float


@dataclass(frozen=True)
class Day:
    """One column of the rhythm strip."""

    date: datetime.date
    count: int


def _share(part: int, whole: int) -> float:
    return min(1.0, part / whole) if whole > 0 else 0.0


def pipeline(view: dict) -> list[Step]:
    """From everything discovered to the applications this app recorded.

    Deliberately stops at what JobDeck did. The register block states the rest,
    because 44 of the rows in it predate this app and calling them "gesendet"
    here would be this screen taking credit for them.
    """
    found = view["jobs_total"]
    drafted, unread_drafts = view["drafted"], view["drafted_unread"]
    applied = view["applied"]
    # Everything the scorer has not reached yet: it is in neither figure below
    # it, so without this the drop from "gefunden" is partly unexplained and
    # the one note under it would be read as the whole reason.
    unscored = max(0, found - view["scored_above_zero"] - view["scored_zero"])
    passend_note = ", ".join(filter(None, [
        f"{view['scored_zero']} verletzen eine harte Anforderung"
        if view["scored_zero"] else "",
        f"{unscored} noch nicht bewertet" if unscored else "",
    ]))
    return [
        Step("gefunden", "gefunden", found, 1.0 if found else 0.0),
        Step("passend", "passen zu einem Profil", view["scored_above_zero"],
             _share(view["scored_above_zero"], found), passend_note),
        Step("angesehen", "von dir geöffnet", view["opened"],
             _share(view["opened"], found)),
        # The two places the column is not a chain, each said where it happens
        # rather than in a caption further away.
        Step("anschreiben", "Anschreiben geschrieben", drafted,
             _share(drafted, found),
             f"{unread_drafts} davon, ohne die Anzeige zu öffnen — der "
             f"Tagesstapel und das Formular schreiben von selbst"
             if unread_drafts else ""),
        Step("beworben", "hier als Bewerbung eingetragen", applied,
             _share(applied, found),
             # An application can be recorded for a posting nothing was ever
             # written for — that is what pressing "Abgeschickt" does on a
             # posting whose letter failed, and how every pre-v10 form
             # application was entered.
             f"{applied - drafted} davon ohne Anschreiben aus JobDeck"
             if applied > drafted else ""),
    ]


def ledger(view: dict) -> list[Step]:
    """The register itself: everything he has ever sent, however it was sent."""
    apps = view["apps"]
    total = len(apps)
    answered = sum(1 for a in apps if _status(a) in BEANTWORTET_STATUS)
    open_rows = sum(1 for a in apps if _status(a) in OFFENE_STATUS)
    # Two tables counted against each other, and nothing constrains them to
    # agree: `total` counts ledger rows, `applied` counts postings pointing at
    # one, and a posting can be repointed or a row edited until the pair drifts.
    # Never print a negative remainder — say nothing rather than something false.
    imported = max(0, total - view["applied"])
    return [
        Step("register", "im Register", total, 1.0 if total else 0.0,
             f"{view['applied']} über JobDeck · {imported} von Hand oder aus "
             f"dem alten Tracker" if imported else ""),
        Step("offen", "noch ohne Antwort", open_rows, _share(open_rows, total)),
        Step("beantwortet", "beantwortet", answered, _share(answered, total)),
    ]


def _status(app: dict) -> str:
    return str(app.get("status") or "")


def _sent_on(app: dict) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(app.get("gesendet_am") or "").strip())
    except ValueError:
        return None


def rhythm(apps: list[dict], today: datetime.date,
           days: int = RHYTHM_DAYS) -> list[Day]:
    """One column per day, oldest first — the pauses are the point.

    `today` is passed in rather than read here: a helper that asked the clock
    itself would make every test of it true only on the day it was written.
    """
    counts: dict[datetime.date, int] = {}
    for app in apps:
        sent = _sent_on(app)
        if sent is not None:
            counts[sent] = counts.get(sent, 0) + 1
    start = today - datetime.timedelta(days=days - 1)
    return [Day(start + datetime.timedelta(days=offset),
                counts.get(start + datetime.timedelta(days=offset), 0))
            for offset in range(days)]


def busiest(strip: list[Day]) -> Day | None:
    """The fullest day of the strip, so the scale can be stated rather than
    guessed at. None when nothing went out at all in the window."""
    peak = max(strip, key=lambda day: day.count, default=None)
    return peak if peak is not None and peak.count else None


def longest_pause(strip: list[Day]) -> int:
    """The longest run of days in the window with nothing sent.

    Counted only BETWEEN two days that had something: a trailing quiet run is
    the present, not a pause, and calling it one would tell him he stopped
    working when he simply has not sent one today.
    """
    active = [index for index, day in enumerate(strip) if day.count]
    if len(active) < 2:
        return 0
    return max(later - earlier - 1
               for earlier, later in zip(active, active[1:], strict=False))


def silence(apps: list[dict], follow_up_days: int,
            today: datetime.date) -> list[Waiting]:
    """Who has not answered, longest first.

    Only applications that are still open: a company that answered is not
    silent, whatever the answer was. A row whose date cannot be read keeps its
    place with `days=None` rather than being dropped or dated to today —
    imported rows exist with no date at all, and inventing one for them would
    sort them among applications sent this morning.
    """
    waiting = []
    for app in apps:
        if _status(app) not in OFFENE_STATUS:
            continue
        sent = _sent_on(app)
        days = (today - sent).days if sent is not None else None
        waiting.append(Waiting(
            bewerbung_id=int(app.get("id") or 0),
            firma=str(app.get("firma") or ""),
            days=days,
            overdue=days is not None and days >= follow_up_days,
            kanal=str(app.get("kanal") or ""),
        ))
    # Unknown ages last: they carry no claim, so they cannot lead a list whose
    # whole order is a claim about age.
    return sorted(waiting, key=lambda row: (row.days is None, -(row.days or 0)))


def by_channel(apps: list[dict]) -> list[Share]:
    """Answered share per application channel, biggest population first."""
    channels: dict[str, list[dict]] = {}
    for app in apps:
        channels.setdefault(str(app.get("kanal") or "—"), []).append(app)
    shares = [
        Share(label=name,
              part=sum(1 for a in rows if _status(a) in BEANTWORTET_STATUS),
              whole=len(rows),
              ratio=_share(sum(1 for a in rows
                               if _status(a) in BEANTWORTET_STATUS), len(rows)))
        for name, rows in channels.items()
    ]
    return sorted(shares, key=lambda s: (-s.whole, s.label))


def by_source(rows: list[dict]) -> list[Share]:
    """Applications per board, against the postings that board delivered.

    Yield, not response rate: only the applications JobDeck made carry a
    source at all, so a response rate here would be computed over a third of
    the register while looking like a statement about all of it.
    """
    shares = [
        Share(label=str(row.get("source") or "—"),
              part=int(row.get("applied") or 0),
              whole=int(row.get("jobs") or 0),
              ratio=_share(int(row.get("applied") or 0), int(row.get("jobs") or 0)))
        for row in rows
    ]
    return sorted(shares, key=lambda s: (-s.whole, s.label))


# What a comparison's bars are allowed to mean. 'ratio' draws the share itself
# — the right choice whenever a percentage is printed beside it. 'whole' draws
# the population, which is the finding when the panel is about what a source
# delivers rather than how well it converts.
BAR_MEASURES = ("ratio", "whole")


def bar_widths(shares: list[Share], measure: str) -> list[float]:
    """Bar widths in 0..1 for one comparison, scaled to its own largest.

    Computed here rather than in the drawing code because a bar is a CLAIM: on
    the answer panel the bar was the population while the figure beside it was
    the rate, so Online-Portal's 41 applications drew a visibly longer bar than
    E-Mail's 35 — directly under a sentence saying the two answer equally often.
    """
    if measure not in BAR_MEASURES:
        raise ValueError(f"a bar may mean {BAR_MEASURES}, not {measure!r}")
    values = [share.ratio if measure == "ratio" else float(share.whole)
              for share in shares]
    widest = max(values, default=0.0)
    return [clamp(value / widest) if widest > 0 else 0.0 for value in values]


def clamp(width: float) -> float:
    """A bar width inside 0..1, whatever arithmetic produced it.

    Every width on the screen goes through here, because the one that did not
    was the one that broke: an application dated in the future gives a
    negative age, `width:-98%` is invalid CSS and is DROPPED, and a block
    element with no width then fills its whole column — so the row furthest
    from being overdue drew the longest bar on the panel.
    """
    if width != width:      # NaN: neither branch of a comparison would catch it
        return 0.0
    return max(0.0, min(1.0, width))


def enough_for_a_rate(shares: list[Share]) -> bool:
    """Whether a comparison of these shares may be stated as percentages.

    EVERY share has to carry the threshold, not the total across them. Summed,
    one application through a third channel rendered "1 beantwortet · 100 %",
    was ranked first, and produced the sentence "Post antwortet häufiger" —
    a finding invented out of a single row, on the panel whose whole purpose
    is to refuse exactly that.
    """
    return bool(shares) and all(
        share.whole >= ENOUGH_FOR_A_RATE for share in shares)


def de_day(day: datetime.date) -> str:
    """'15. Juni' — the rhythm strip's ends, in the screen's own language."""
    return f"{day.day}. {MONATE_DE[day.month]}"


# The setting this screen PRINTS and colours by, which no table signature can
# see. The rail learned this the hard way: connecting Gmail on the page beside
# it left the rail reading "Gmail fehlt" for the life of that page.
_WATCHED_SETTINGS = ("follow_up_days",)


def signature(con) -> tuple:
    """Everything this screen shows, cheaply comparable (see ui/live.py).

    Wider than the pipeline: the silence panel states the threshold, sorts by
    it and colours by it, so raising it in Einstellungen has to reach this
    screen — otherwise the number beside "Ab N Tagen" and the rows beneath it
    describe two different settings until the page is reloaded.
    """
    return (*db.data_signature(con),
            *(db.get_setting(con, key, "") for key in _WATCHED_SETTINGS))


def facts() -> dict:
    """Everything the Bewerbungen screen states, in one read."""
    with db.db() as con:
        # First, before a single count: sqlite3 gives every SELECT its own
        # snapshot, so a write landing between them would marry stale rows to
        # a fresh signature and the watcher would record that as current.
        sig = signature(con)
        counts = db.pipeline_counts(con)
        return {
            "signature": sig,
            "apps": [dict(row) for row in db.list_bewerbungen(con)],
            "sources": [dict(row) for row in db.applications_by_source(con)],
            "follow_up_days": follow_up_setting(
                db.get_setting(con, "follow_up_days", "")),
            **counts,
        }


# After how many silent days an application is worth chasing, when he has
# never said. The same default the rail uses, read the same forgiving way: a
# setting is free text in a table he can edit, and `int("")` taking down the
# screen is the shape that once took down the inbox over an age threshold.
FOLLOW_UP_DEFAULT = 14


def follow_up_setting(raw: str) -> int:
    """The stored threshold as a whole number of days, never raising."""
    try:
        days = int(float(str(raw).strip()))
    except (TypeError, ValueError, OverflowError):
        return FOLLOW_UP_DEFAULT
    return days if days > 0 else FOLLOW_UP_DEFAULT
