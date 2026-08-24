"""Whether an application may be made for a posting, and why not.

One decision point, consulted by every path that can create an application —
the e-mail send, the form recorder, the manual entry — and by the screens that
have to explain themselves. Two copies of this rule would let a screen say one
thing while the gate does another, which is the failure this module exists to
make impossible.

The policy is `docs/adr/0010-company-cooling-off-window.md`, which supersedes
the "warn and confirm on another position" arm of ADR 0002. In short:

* a republication of a posting that already became an application is blocked
  for good — applying twice to the same position is a mistake no window makes
  reasonable;
* any other posting at a company written to inside the cooling-off window is
  held back until the window passes, then returns on its own;
* a shared contact address is corroborating evidence and never an identity of
  its own, so it can annotate a decision but never make one.

The module is pure: plain records in, one decision out. It holds no SQL and no
wording, so the identity corpus can drive it directly and both the SQLite
filter and the UI can be checked against the same answers.
"""

from __future__ import annotations

import dataclasses
import datetime

from jobdeck.dedupe import norm

# Nothing known about this company stands in the way.
ALLOW = "allow"
# The same position at the same company already went out. Permanent: the
# window is about not crowding an employer, not about forgetting what was
# sent.
BLOCKED_REPUBLICATION = "blocked_republication"
# An application to this company is recent enough that another one now would
# arrive on top of it. Temporary, and the decision carries the day it lifts.
COOLING_OFF = "cooling_off"
# Another path is mid-flight for this company right now. Not a policy verdict
# at all — a live reservation, held only for the seconds a provider call takes.
RESERVED = "reserved"

# 0 switches the window off entirely, the same way `silence_closes_after_days`
# does, and so does any value below it: a hand-edited negative means "stop
# holding companies back" far more plausibly than it means any number of days.
# Nothing is transmitted by a posting becoming visible, so the widening this
# allows is a listing, never a send.
WINDOW_OFF = 0

# The candidate setting that carries the window, and the default the product
# ships with. Here rather than beside the database access, because both the
# gate and the SQL filter that mirrors it have to read the same number.
COOLDOWN_SETTING = "company_cooldown_days"
DEFAULT_COOLDOWN_DAYS = 60


@dataclasses.dataclass(frozen=True)
class Application:
    """One row of the ledger, as identity needs to see it.

    `position` is empty when the ledger cannot say which role was applied
    for — in a real corpus, 44 of 131 rows, because they were recorded by hand
    or their posting is gone. Empty means UNKNOWN, never "no position": a
    decision may not treat it as proof that this is a different role.
    """

    id: int
    company: str
    email: str = ""
    position: str = ""
    sent_on: str = ""
    # When the employer was last in touch: a receipt they sent, or the day the
    # application went out when there was none. The window is counted from
    # HERE, not from `sent_on` — a ledger row can be months old while the
    # conversation is days old, and then counting from the send date offers a
    # company that answered last week.
    last_contact: str = ""

    @property
    def anchor(self) -> str:
        return str(self.last_contact or self.sent_on or "")


@dataclasses.dataclass(frozen=True)
class Reservation:
    """A live claim on a company, held by whichever path is mid-flight."""

    key: str
    company: str
    channel: str = ""
    job_id: int | None = None


@dataclasses.dataclass(frozen=True)
class Posting:
    """The posting being judged."""

    company: str
    title: str = ""
    contact_email: str = ""
    job_id: int | None = None


@dataclasses.dataclass(frozen=True)
class Decision:
    """A verdict that carries the evidence it was reached on.

    ADR 0002 requires identity decisions to retain evidence and confidence,
    so a screen can say WHICH application stands in the way and WHEN it stops
    doing so rather than only that something does.
    """

    verdict: str
    application_id: int | None = None
    position: str = ""
    sent_on: str = ""
    # The date the window was actually counted from. Carried separately from
    # `sent_on` so a screen can say "last contact on X" instead of claiming an
    # application was made on a day the ledger does not hold.
    last_contact: str = ""
    reopens_on: str = ""
    reservation_key: str = ""
    corroborating_email: bool = False

    @property
    def allowed(self) -> bool:
        return self.verdict == ALLOW

    @property
    def permanent(self) -> bool:
        """True when waiting cannot change the answer."""
        return self.verdict == BLOCKED_REPUBLICATION


def _date(raw: object) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(raw or "")[:10])
    except ValueError:
        return None


def reopens_on(sent_on: object, window_days: int) -> str:
    """The first day a company written to on `sent_on` is offered again.

    Empty when the ledger row carries no usable date. That is not cosmetic:
    an undated application cannot prove its window has passed, so the caller
    must keep holding the company back rather than assume it is free.
    """
    day = _date(sent_on)
    if day is None or window_days <= WINDOW_OFF:
        return ""
    return (day + datetime.timedelta(days=window_days)).isoformat()


def republication_of(
    at_company: list[Application], title: str
) -> Application | None:
    """The application this posting would be a second copy of, or None.

    Separate from `decide` for the same reason `holds_company` is: the SQL
    filter that decides whether a posting is LISTED asks exactly this, while
    `decide` also weighs a window and a live reservation. Two questions, one
    rule, so a differential test can pin the filter and the gate to the same
    statement instead of to two that merely resemble each other.

    Both titles must be non-empty. Two postings with no title are not the same
    role, they are two rows that failed to store one — and an application whose
    position is unknown may never be read as proof that a posting repeats it.
    """
    key = norm(title)
    if not key:
        return None
    for application in at_company:
        if norm(application.position) == key:
            return application
    return None


def holds_company(
    at_company: list[Application],
    *,
    window_days: int,
    today: datetime.date | None = None,
) -> Application | None:
    """The application still holding this company, or None.

    Narrower than `decide` on purpose, and separate from it for one reason:
    the SQL filter that decides whether a posting is LISTED answers exactly
    this question, while `decide` answers what happens if an application is
    attempted — a republication is refused whether or not its company is
    still held. Two questions, one rule, so a differential test can pin the
    filter and the gate to the same statement instead of to two that merely
    resemble each other.

    The most recent contact decides: an older application at the same company
    has already been superseded, and the window is about how long ago that
    employer was last in touch — which is not always the day something was
    sent to them.
    """
    now = today or datetime.date.today()
    if window_days <= WINDOW_OFF or not at_company:
        return None
    newest = max(at_company, key=lambda a: (a.anchor, a.id))
    opens = reopens_on(newest.anchor, window_days)
    # No usable date means the window cannot be proven to have passed. Held,
    # with an empty `reopens_on` so the screen says so instead of printing a
    # day it invented.
    if not opens or _date(opens) > now:
        return newest
    return None


def decide(
    posting: Posting,
    applications: list[Application],
    reservations: list[Reservation] = (),
    *,
    window_days: int,
    today: datetime.date | None = None,
) -> Decision:
    """Judge one posting against the ledger and the live reservations.

    Order matters and is not arbitrary. A republication is checked first
    because it is the only permanent answer, so a company that is also inside
    its window must still be told the honest reason. Reservations come next:
    they are about this instant rather than about policy, and a caller holding
    one must not be told a story about days. The window is last.
    """
    now = today or datetime.date.today()
    company_key = norm(posting.company)
    email_key = norm(posting.contact_email)

    # A blank company is missing information, not an employer — the same
    # reading `_COMPANY_KEY_SQL` already takes, where such a posting gets a key
    # of its own. Without this every unnamed posting would collide with every
    # other one and with any ledger row whose company failed to store.
    if not company_key:
        return Decision(verdict=ALLOW)

    at_company = [a for a in applications if norm(a.company) == company_key]

    # Same company AND same position: this posting is the one already sent, or
    # the employer's repost of it. `title_key` must be non-empty on both sides
    # — two postings with no title are not the same role, they are two rows
    # that failed to store one.
    repeat = republication_of(at_company, posting.title)
    if repeat is not None:
        return Decision(
            verdict=BLOCKED_REPUBLICATION,
            application_id=repeat.id,
            position=repeat.position,
            sent_on=repeat.sent_on,
        )

    for reservation in reservations:
        if norm(reservation.company) == company_key:
            return Decision(
                verdict=RESERVED, reservation_key=reservation.key
            )

    # An address shared with a ledger row is worth showing — an ATS mailbox
    # serving two employers, a recruiter fronting for one — but ADR 0002 is
    # explicit that it can never be the identity itself. It rides along on the
    # decision and changes no verdict.
    corroborating = bool(
        email_key
        and any(
            norm(a.email) == email_key
            for a in applications
            if norm(a.company) != company_key
        )
    )

    holding = holds_company(at_company, window_days=window_days, today=now)
    if holding is not None:
        return Decision(
            verdict=COOLING_OFF,
            application_id=holding.id,
            position=holding.position,
            sent_on=holding.sent_on,
            last_contact=holding.anchor,
            reopens_on=reopens_on(holding.anchor, window_days),
            corroborating_email=corroborating,
        )

    return Decision(verdict=ALLOW, corroborating_email=corroborating)
