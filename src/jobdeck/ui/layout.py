"""The frame every screen is drawn in: the rail on the left, the screen beside it.

There is no header bar. The old one repeated the app's name and the page's name
above a drawer that already said both, which cost a strip of vertical space on
every screen and told him nothing — and vertical space is what a list of
postings and the text of a posting are both short of. The rail carries the name
once, and what used to be a title is now the browser tab.
"""

from contextlib import asynccontextmanager

from nicegui import ui

from jobdeck.ui import rail, theme

# The rail's own width, from the approved design. Wide enough for a rubric's
# figure to sit on the same line as its name, narrow enough that the screen
# beside it still holds a list and a reading pane.
RAIL_WIDTH = 238


# What a screen that lays itself out gets, against what a screen of stacked
# cards gets. Stellen fills the viewport with a list and a reading pane and has
# to own every pixel; the screens still waiting for their own slice keep the
# centred column they were written for.
FULL_BLEED = "w-full p-0 gap-0"
PADDED = "w-full max-w-6xl mx-auto p-4 gap-4"


# The Bewerbungen rubric, in the order his decision put them: the stack that
# drains first, the record that only grows second.
BEWERBUNGEN_TABS = [
    ("postausgang", "Postausgang", rail.POSTAUSGANG_PATH),
    ("register", "Register", rail.BEWERBUNGEN_PATH),
]


def tabs(current: str, entries: list[tuple[str, str, str]]) -> None:
    """The faces of one rubric, as a strip at the top of each of them.

    Bewerbungen is two screens: the stack of letters still to send, and the
    register of everything that went. They stayed two ROUTES rather than
    becoming one page with a switch — a bookmark keeps working, and the send
    path is the riskiest code in the app to relocate for a layout — so this
    strip is what makes them read as one rubric.

    `entries` is (key, label, path); the current one is marked, not linked.

    Every path is checked to be an in-app route. `ui.navigate.to` becomes
    window.open in the app's own origin, and the rule that guards that
    elsewhere refuses a call that SUBSCRIPTS its way to a target — which this
    helper does not do, because its target arrives as a plain parameter. A
    generic navigator therefore has to carry its own gate, or the next caller
    can hand it a stored employer URL and the AST rule will not notice.
    """
    # Before a single element is drawn: a strip that raised halfway would
    # leave half a rubric on screen and the rest of the page unbuilt.
    for _key, _label, path in entries:
        if not _is_internal(path):
            raise ValueError(f"a tab may only open an in-app route: {path!r}")
    with ui.row().classes("jd-tabs w-full gap-1 items-center"):
        for key, label, path in entries:
            element = ui.element("button").classes("jd-tab").props(
                f'data-current={"true" if key == current else "false"}') \
                .mark(f"tab-{key}")
            if key != current:
                element.on("click", lambda _=None, p=path: ui.navigate.to(p))
            with element:
                ui.label(label)


def _is_internal(path: str) -> bool:
    """A single-slash-rooted path of this app, and nothing that can leave it.

    `//host` is protocol-relative and leaves the origin; a backslash is
    normalised to a slash by browsers, so `/\\host` leaves it too; a colon
    before the first slash is a scheme.
    """
    # The browser REMOVES every ASCII tab, LF and CR before it parses a URL
    # (WHATWG), so "/\t/evil.example/x" reaches window.open as
    # "//evil.example/x" — protocol-relative, and off this origin. Strip them
    # the way the consumer will before judging, then judge.
    text = "".join(ch for ch in str(path or "") if ch not in "\t\n\r")
    return (text.startswith("/")
            and not text.startswith(("//", "/\\"))
            and ":" not in text.split("/")[0])


@asynccontextmanager
async def frame(title: str, current: str = "", padded: bool = True,
                shelf: bool = True):
    """Standard page scaffolding. `current` is the rubric key the rail marks.

    Asynchronous because the rail reads the database, and every sqlite call
    from async context goes to a worker thread. Handing that read to a timer
    instead was tried and is worse: a one-shot timer firing between the page
    function's own awaits cancelled the page build, which came back as a screen
    holding nothing but this frame."""
    theme.install()
    ui.page_title(f"{title} · JobDeck" if title else "JobDeck")
    with ui.left_drawer(value=True, fixed=True) \
            .classes("jd-rail flex flex-col p-0") \
            .props(f"width={RAIL_WIDTH} bordered"):
        await rail.install(current, with_shelf=shelf)
    with ui.column().classes(PADDED if padded else FULL_BLEED):
        yield
