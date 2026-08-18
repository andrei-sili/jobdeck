"""Close applications that nobody ever answered.

An application he sent and heard nothing about is not refused — nobody
refused it. It is also not open: after two months the seat is filled and the
row is only making the register harder to read. So it closes itself as
"Keine Antwort", which says exactly that and nothing more.

Two properties are deliberate and load-bearing:

* The status is NOT "Absage". Absage means an employer decided, and it feeds
  the response-rate metric — measured on the real register, folding silence
  into it moved the rate from 32% to 50% with nobody having answered.
* It loses to a real verdict. "Keine Antwort" ranks below an invitation or a
  rejection, so an employer who answers on day 65 still writes the truth over
  it automatically, with no click from him.

Nothing is deleted and nothing is sent: this pass only moves a status, and
every move lands in `status_history` with its own source.
"""

import asyncio
import logging

from jobdeck import db
from jobdeck.constants import DEFAULT_SILENCE_CLOSES_DAYS

log = logging.getLogger(__name__)

STATUS_NO_ANSWER = "Keine Antwort"
SOURCE = "silence"
SETTING_DAYS = "silence_closes_after_days"

# 0 (or anything unparseable) switches the rule off rather than closing
# everything the moment the setting is cleared.
OFF = 0


def configured_days(con) -> int:
    """The silence window, as he set it. 0 means the rule is off."""
    raw = db.get_setting(con, SETTING_DAYS, str(DEFAULT_SILENCE_CLOSES_DAYS))
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_SILENCE_CLOSES_DAYS
    return max(OFF, days)


def _close_silent() -> dict:
    """Close every application past the window. Returns a small report."""
    closed, refused = [], []
    with db.db() as con:
        days = configured_days(con)
        if days == OFF:
            return {"days": OFF, "closed": [], "refused": [], "off": True}
        for row in db.silent_applications(con, days):
            applied = db.set_status(
                con, row["id"], STATUS_NO_ANSWER, SOURCE,
                note=f"seit {row['last_contact'][:10]} ohne Antwort",
            )
            (closed if applied else refused).append(row["firma"])
        con.commit()
    if closed:
        log.info("silence: closed %d application(s) after %d days: %s",
                 len(closed), days, ", ".join(sorted(closed)))
    if refused:
        # The rank guard refused the write — the row already carries a verdict
        # this pass must not overwrite. Worth a line, never an error.
        log.info("silence: left %d application(s) alone: %s",
                 len(refused), ", ".join(sorted(refused)))
    return {"days": days, "closed": closed, "refused": refused, "off": False}


async def close_silent() -> dict:
    """Run the closing pass off the event loop."""
    return await asyncio.to_thread(_close_silent)
