"""What the Bewerbungen screen states, computed before anything is drawn.

Pure functions over rows, in the shape `ui/rail.py` established: the numbers
are derived here and rendered second, so every claim the screen makes can be
pinned without a browser — and so a claim and its bar cannot disagree.

Two honesty rules govern this module, both forced by his real register:

* **The parts add up, or they say what is left over.** The card leads with a
  total and splits it twice, and a reader who cannot make the parts reach the
  whole stops trusting every other figure on the screen. Both remainders are
  therefore computed rather than enumerated — a status added to the
  vocabulary tomorrow cannot silently fall out of every line.
* **The register is older than the app.** 44 of his applications were
  imported from the tracker he kept before JobDeck existed. A screen that
  called all of them "gesendet" would be claiming credit for them, so what
  this app did and what the register holds are two separate blocks — and it
  is why the answer-time sample can never equal the figure above it: a
  rejection recorded by hand carries no mail to measure.
"""

import datetime
import math
import statistics
from dataclasses import dataclass

from jobdeck import db
from jobdeck import settings as app_settings
from jobdeck.constants import (
    BEANTWORTET_STATUS,
    DEFAULT_FOLLOW_UP_DAYS,
    OFFENE_STATUS,
    STATUS_NO_ANSWER,
)
from jobdeck.dates import MONATE_DE, silence_anchor

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


def plural(count: int, one: str, many: str, tail: str = "") -> str:
    """"1 Tag Pause" and "36 Tage Pause" — never "1 Tage Pause".

    Every figure on this screen can be one. Six German sentences were written
    only in the plural, so a register holding a single application read
    "1 Bewerbungen ohne Antwort", "mit 1 Bewerbungen" and "1 davon sind über
    der Schwelle" — the kind of German that makes a reader distrust the
    number beside it. Returns '' for zero, so a caller can drop the note.
    """
    if not count:
        return ""
    return f"{count} {one if count == 1 else many}{tail}"


def _share(part: int, whole: int) -> float:
    return min(1.0, part / whole) if whole > 0 else 0.0


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
    # An application closed for silence is in neither of the two lines below —
    # nobody answered it, and it is no longer waiting. Without its own line the
    # register's parts stop adding up to its total, on the one screen whose
    # whole value is that its numbers are honest.
    unanswered = sum(1 for a in apps if _status(a) == STATUS_NO_ANSWER)
    steps = [
        Step("register", "im Register", total, 1.0 if total else 0.0,
             f"{view['applied']} über JobDeck · {imported} von Hand oder aus "
             f"der alten Liste" if imported else ""),
        Step("offen", "noch ohne Antwort", open_rows, _share(open_rows, total)),
        Step("beantwortet", "beantwortet", answered, _share(answered, total)),
    ]
    if unanswered:
        steps.append(
            Step("ohne_antwort", "ohne Antwort geschlossen", unanswered,
                 _share(unanswered, total),
                 "niemand hat abgesagt — es kam nur nie eine Antwort"))
    # The REMAINDER, not a fourth status set — so the parts add up by
    # construction rather than by two vocabularies staying in step.
    # "Zurückgezogen" is in STATUS_OPTIONS and in none of the three sets
    # above, and the edit dialog on THIS screen offers it; `add_bewerbung`
    # defaults a missing status to ''. Either one silently left the register
    # split into parts that no longer summed to the total printed over them,
    # on the card whose entire premise is that they do.
    rest = total - open_rows - answered - unanswered
    if rest > 0:
        steps.append(
            Step("sonst", "zurückgezogen oder ohne Stand", rest,
                 _share(rest, total),
                 "weder offen noch beantwortet — sie warten auf nichts"))
    return steps


def answers(apps: list[dict]) -> list[Step]:
    """What the answers actually were — the breakdown the register bridges to.

    Counted from the CURRENT status, so the three add up to "beantwortet"
    exactly. Counting invitations ever REACHED was measured and rejected: on
    the real register that number is one, and that one is the residue of a
    classifier which read a quoted sentence out of his own application and
    wrote "Einladung" by itself before being corrected. A figure whose only
    non-zero value is a fixed bug is worse than an honest zero.

    A zero IS printed here, unlike the waiting-for-a-score line on Stellen.
    The difference is what the number is about: a background worker's empty
    queue is nothing to report, and no invitations yet is the score.
    """
    answered = [a for a in apps if _status(a) in BEANTWORTET_STATUS]
    counted = {
        "einladung": sum(1 for a in answered if _status(a) == "Einladung"),
        "absage": sum(1 for a in answered if _status(a) == "Absage"),
        # Everything else a person wrote back. Named for what it is rather
        # than left out: without it the three figures stop adding up to the
        # number directly above them, on the one screen whose entire value is
        # that its parts agree.
        "sonstige": sum(1 for a in answered
                        if _status(a) not in ("Einladung", "Absage")),
    }
    # Bars measured against the WHOLE REGISTER, exactly like the group above.
    # Both groups render through one `_funnel_row` into sibling grids whose
    # bar tracks line up in a single visual column, so two scales put a
    # smaller figure under a longer bar: "Absagen 35" drew at 95 % of the
    # width two rows under "beantwortet 37" at 26 %. One meaning for one
    # column — every bar here is a share of the register.
    whole = len(apps)
    return [
        Step("einladung", "Einladungen", counted["einladung"],
             _share(counted["einladung"], whole)),
        Step("absage", "Absagen", counted["absage"],
             _share(counted["absage"], whole)),
        Step("sonstige", "sonstige Antworten", counted["sonstige"],
             _share(counted["sonstige"], whole)),
    ]


# Below this many measured answers a median is a coincidence rather than a
# finding. Deliberately lower than ENOUGH_FOR_A_RATE: a rate has to be stable
# against one more outcome flipping it, and a middle value does not.
ENOUGH_FOR_A_TIME = 8


def answer_days(pairs: list[tuple[str, str]]) -> list[int]:
    """Whole days from sending to a decision, sorted, for the pairs that carry
    two readable dates.

    A negative span is dropped rather than clamped to zero: it means the two
    dates describe different things (an imported row dated by hand, a reply
    threaded onto the wrong application), and folding it into the middle of
    the distribution would quietly pull the answer down.
    """
    days = []
    for sent, answered in pairs:
        try:
            first = datetime.date.fromisoformat(str(sent)[:10])
            last = datetime.date.fromisoformat(str(answered)[:10])
        except (TypeError, ValueError):
            continue
        if (last - first).days >= 0:
            days.append((last - first).days)
    return sorted(days)


def answer_time(pairs: list[tuple[str, str]],
                answered: int) -> tuple[str, str]:
    """(the sentence, what it was measured over) — ('', '') when too few.

    The middle and the slowest, not the average: one reply after two months
    drags a mean far past anything he has actually experienced, and the
    question this answers is "how long before I stop expecting one".

    `answered` is the figure directly above on the card, and it is required
    rather than defaulted: a caller that did not have to state it is a caller
    that can print a sample larger than the population it is drawn from. The second line
    relates the sample to it rather than naming a bare population, because
    the two can never be equal: an application he marked "Absage" by hand
    carries no mail at all, and 44 of the rows in his register predate this
    app. Saying "N Antworten" also counted the wrong noun — the query groups
    by application, so N is applications, and the sentence said replies.
    """
    days = answer_days(pairs)
    if len(days) < ENOUGH_FOR_A_TIME:
        return "", ""
    # Round half UP, not int() and not round(). Truncation floors the median
    # of an even-sized sample, and it floors it in the flattering direction
    # every time; Python's round() breaks a tie to even, which on integer day
    # counts is a tie half the time and flatters on half of those. A wait is
    # reported long rather than short, on the card whose whole claim is that
    # its figures are honest.
    middle, slowest = math.floor(statistics.median(days) + 0.5), days[-1]
    sentence = f"Im Median kam eine Antwort {_after_days(middle)}"
    # Only when it says something new. With every measured answer the same
    # age, "die langsamste nach 4 Tagen" repeats the clause before it — and a
    # sentence that states one fact twice is one a reader starts skimming.
    sentence += (f", die langsamste {_after_days(slowest)}."
                 if slowest != middle else ".")
    rest = max(0, answered - len(days))
    over = (f"Gemessen an {len(days)} der {answered} beantworteten "
            f"Bewerbungen" +
            (f" — bei den übrigen {rest} kam die Antwort nicht per E-Mail an."
             if rest else " — Eingangsbestätigungen zählen nicht mit."))
    return sentence, over


def _after_days(days: int) -> str:
    """'noch am selben Tag', 'nach einem Tag', 'nach 4 Tagen'.

    The preposition belongs to the phrase, because the same-day case does not
    take one: a wait of nought days is not a wait, and "nach 0 Tagen" is the
    arithmetic showing through the German. Its own helper rather than
    `plural`, which prefixes the figure — the singular here replaces it.
    """
    if days <= 0:
        return "noch am selben Tag"
    return "nach einem Tag" if days == 1 else f"nach {days} Tagen"


def _status(app: dict) -> str:
    return str(app.get("status") or "")


def _sent_on(app: dict) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(app.get("gesendet_am") or "").strip())
    except ValueError:
        return None


def _silent_since(app: dict) -> datetime.date | None:
    """The date the closing rule counts from — the last contact, not the send.

    `last_contact` is carried by `db.list_bewerbungen` so this screen and
    `db.silent_applications` cannot disagree about the same application. It
    falls back to the send date when a caller builds a row by hand, which is
    also what the SQL does when no inbound mail exists.
    """
    raw = silence_anchor(app)
    if raw:
        try:
            return datetime.date.fromisoformat(raw[:10])
        except ValueError:
            pass
    return _sent_on(app)


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

    Counted from the LAST CONTACT, exactly as the closing rule counts it. A
    receipt does not answer anything but it does prove someone is there, and a
    screen that counted from the send date instead printed "69 T" beside a
    threshold of 60 on a row the rule would not close.
    """
    waiting = []
    for app in apps:
        if _status(app) not in OFFENE_STATUS:
            continue
        since = _silent_since(app)
        days = (today - since).days if since is not None else None
        waiting.append(Waiting(
            bewerbung_id=int(app.get("id") or 0),
            firma=str(app.get("firma") or ""),
            days=days,
            overdue=days is not None and days >= follow_up_days,
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


# What a board is CALLED, against the adapter key it is stored under. The
# panel printed "arbeitsagentur · 285" — a lowercase technical identifier in
# an otherwise German screen. The full legal names live in `apply_form` for
# the "Wie haben Sie von uns erfahren?" field; a panel needs the short one.
SOURCE_NAMES = {
    "arbeitsagentur": "Arbeitsagentur",
    "jooble": "Jooble",
    "arbeitnow": "Arbeitnow",
}


def source_name(key: str) -> str:
    return SOURCE_NAMES.get(key, key)


def by_source(rows: list[dict]) -> list[Share]:
    """Applications per board, against the postings that board delivered.

    Yield, not response rate: only the applications JobDeck made carry a
    source at all, so a response rate here would be computed over a third of
    the register while looking like a statement about all of it.
    """
    shares = [
        Share(label=source_name(str(row.get("source") or "—")),
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

    Wider than the tables alone: the silence panel states the threshold, sorts
    by it and colours by it, so raising it in Einstellungen has to reach this
    screen — otherwise the number beside "Ab N Tagen" and the rows beneath it
    describe two different settings until the page is reloaded.

    The answer-time sentence added two more inputs, and `db.data_signature`
    had to learn to see both: a reply RECLASSIFIED between two non-empty
    values, and a send date corrected on any row but the newest.
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
        return {
            "signature": sig,
            "apps": [dict(row) for row in db.list_bewerbungen(con)],
            "sources": [dict(row) for row in db.applications_by_source(con)],
            "follow_up_days": follow_up_setting(
                db.get_setting(con, "follow_up_days", "")),
            # When each application FIRST entered an answered status — from
            # status_history, so a hand-recorded Absage and an ingested one
            # carry the same kind of date under the same column head.
            "answer_dates": db.first_answer_dates(con),
            # The aggregate uses the MAIL's date where the per-row column
            # above uses the recording moment — two clocks on purpose, and
            # `db.answer_delays` states why one claim needs each.
            "answer_delays": db.answer_delays(con),
            "applied": db.count_applied_postings(con),
        }


# After how many silent days an application is worth chasing, when he has
# never said. Imported rather than repeated: it was declared here as 14 with a
# comment claiming it was "the same default the rail uses", and the only test
# compared it to itself — so it could be moved to 99 with the suite green
# while the rail went on saying 14 about the same applications.
FOLLOW_UP_DEFAULT = DEFAULT_FOLLOW_UP_DAYS


def follow_up_setting(raw: str) -> int:
    """The stored threshold as a whole number of days, never raising."""
    return app_settings.parse_int(
        raw,
        FOLLOW_UP_DEFAULT,
        minimum=1,
        allow_decimal=True,
        clamp=False,
    )
