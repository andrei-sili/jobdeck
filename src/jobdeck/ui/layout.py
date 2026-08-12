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


@asynccontextmanager
async def frame(title: str, current: str = "", padded: bool = True):
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
        await rail.install(current)
    with ui.column().classes(PADDED if padded else FULL_BLEED):
        yield
