"""Pages that re-read themselves while he is looking at them.

The engine moves without anyone clicking: every search profile polls hourly,
scoring runs every ten minutes, the liveness pass 90 seconds after each start.
Until now only the review queue noticed — every other page rendered once and
then described a database that had moved on, which is the literal complaint
("nu se actualizeaza"): postings arrive invisibly, a finished draft keeps
promising a minute, an ad dies under a row that still offers to apply to it.

This is the queue's proven mechanism, made shared, plus the one rule the queue
could do without. Three properties, each earned by a defect:

* **Signature-gated.** A rebuild collapses every open expansion, so a page is
  rebuilt only when a cheap query says the data really changed — never on a
  timer alone.
* **Deferred while he is busy.** A dialog over the list, or a posting he has
  expanded to read, means the fresh data waits behind a chip instead of being
  yanked out from under him. No tick is lost: once the page is idle again the
  next one applies it by itself.
* **Cancelled on disconnect.** NiceGUI reads a timer's parent slot BEFORE its
  own "should I stop?" test, so a tick landing after the page is torn down
  raises — an ERROR traceback in the log every time he leaves the page.

A page supplies two things: a `signature` callable that answers "has anything
I display changed?" in one cheap query, and its own `refresh`. The signature is
compared, never interpreted, so any hashable value works; a page reads it
alongside its own data and hands it back through `mark()`, which is what keeps
a rebuild the page did itself from being repeated by the next tick.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from nicegui import run, ui

log = logging.getLogger(__name__)

# How often a page asks whether anything changed. Half a minute sits far below
# the fastest thing that moves in the background (scoring, every ten minutes)
# and far above the cost of the question (one indexed aggregate).
SLOW_SECONDS = 30.0

# What the chip says while fresh data waits for him to finish reading.
NOTICE_LABEL = "Neue Daten – aktualisieren"

# Actions a tick can take, as words rather than booleans — the three cases are
# what the tests pin, and "rebuild" vs "defer" is the whole point of the class.
IDLE, DEFER, REBUILD = "idle", "defer", "rebuild"


def should_check(tick: int, hot: bool, idle_every: int) -> bool:
    """Whether this tick asks the database anything at all.

    A page that needs a fast beat for one state (the queue watching a draft
    being written) must not pay for that beat forever: while nothing is hot,
    only every `idle_every`-th tick asks. A gate derived from the last render
    alone would never START polling for something begun in another tab and
    never STOP for a claim stranded by a crash, so the slow beat covers both.
    """
    return hot or idle_every <= 1 or tick % idle_every == 0


def tick_action(changed: bool, busy: bool) -> str:
    """What a tick does once it knows the two facts that decide it."""
    if not changed:
        return IDLE
    return DEFER if busy else REBUILD


def dialog_open(host: ui.element) -> bool:
    """True while a dialog built inside `host` is on screen.

    Pages build their dialogs in an overlay element that no refresh deletes and
    clear it before opening the next one, so the host holds at most the last
    dialog — closed ones stay as elements, which is why this asks each one
    whether it is OPEN rather than whether one exists.
    """
    return any(isinstance(el, ui.dialog) and el.value
               for el in host.descendants())


class LiveView:
    """A page's self-refresh: poll a signature, rebuild when it is safe to."""

    def __init__(
        self,
        signature: Callable[[], Any],
        refresh: Callable[[], Awaitable[None]],
        *,
        seconds: float = SLOW_SECONDS,
        busy: Callable[[], bool] | None = None,
        hot: Callable[[], bool] | None = None,
        idle_every: int = 1,
        label: str = NOTICE_LABEL,
    ) -> None:
        self._signature = signature
        self._refresh = refresh
        self._busy = busy
        self._hot = hot
        self._idle_every = max(1, idle_every)
        self._seen: Any = None
        self._ticks = 0
        self.notice = ui.button(label, icon="refresh", on_click=self.apply) \
            .props("flat dense").classes("text-xs")
        self.notice.set_visibility(False)
        self.timer = ui.timer(seconds, self.poll)
        # The lambda absorbs the client NiceGUI hands a one-parameter handler;
        # `timer.cancel` takes none.
        ui.context.client.on_disconnect(lambda *_: self.timer.cancel())

    def mark(self, signature: Any) -> None:
        """Record what the page is now showing, and put the chip away.

        Called from the page's own refresh with the signature read beside the
        data. Without it every write the user makes himself (drafting, skipping,
        recording an application) would be seen by the next tick as somebody
        else's change and rebuild the page a second time — collapsing the row he
        had just acted on."""
        self._seen = signature
        self.notice.set_visibility(False)

    async def apply(self) -> None:
        """Take the waiting data now — the chip's own handler."""
        await self._refresh()
        self.notice.set_visibility(False)

    async def poll(self) -> None:
        self._ticks += 1
        hot = bool(self._hot()) if self._hot is not None else False
        if not should_check(self._ticks, hot, self._idle_every):
            return
        signature = await run.io_bound(self._signature)
        busy = bool(self._busy()) if self._busy is not None else False
        action = tick_action(signature != self._seen, busy)
        if action == IDLE:
            return
        if action == DEFER:
            # He is reading something. The data is not lost: this same
            # comparison runs again in `seconds`, and the moment the dialog or
            # the expansion is closed it rebuilds by itself.
            self.notice.set_visibility(True)
            return
        # Recorded BEFORE the rebuild so a page that forgets to `mark()` still
        # cannot be rebuilt on every tick forever; the page's own mark, read
        # beside its data, then supersedes this one.
        self._seen = signature
        await self._refresh()


def watch(
    signature: Callable[[], Any],
    refresh: Callable[[], Awaitable[None]],
    **kwargs: Any,
) -> LiveView:
    """Start a page's self-refresh, placing its chip in the current slot."""
    return LiveView(signature, refresh, **kwargs)
