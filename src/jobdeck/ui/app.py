"""Application wiring: startup, scheduler lifecycle, page registration."""

import logging

from nicegui import app, run, ui

from jobdeck import config, db
from jobdeck.scheduler import create_scheduler, shutdown_scheduler

# Importing the page modules registers their @ui.page routes.
from jobdeck.ui.pages import (  # noqa: F401
    applications,
    dashboard,
    jobs,
    profiles,
    settings,
)

log = logging.getLogger(__name__)

startup_warning: str | None = None


async def _startup() -> None:
    global startup_warning
    startup_warning = await run.io_bound(db.bootstrap)
    if startup_warning:
        log.warning("startup backup warning: %s", startup_warning)
    create_scheduler().start()
    log.info("JobDeck started, data dir: %s", config.DATA_DIR)


def _shutdown() -> None:
    shutdown_scheduler()


def run_app(native: bool = False) -> None:
    logging.basicConfig(level=logging.INFO)
    config.ensure_data_dirs()
    config.load_env()
    app.on_startup(_startup)
    app.on_shutdown(_shutdown)
    ui.run(
        title="JobDeck",
        port=8123,
        reload=False,  # reload would double-start the scheduler
        show=True,
        native=native,
        storage_secret=None,
    )
