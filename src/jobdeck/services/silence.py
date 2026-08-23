"""Close applications that nobody ever answered.

An application he sent and heard nothing about is not refused — nobody
refused it. It is also not open: after two months the seat is filled and the
row is only making the register harder to read. So it closes itself as
"Keine Antwort", which says exactly that and nothing more.

Three properties are deliberate and load-bearing:

* The status is NOT "Absage". Absage means an employer decided, and it feeds
  the response-rate metric — measured on the real register, folding silence
  into it moved the rate from 32% to 50% with nobody having answered.
* It loses to a real verdict. "Keine Antwort" ranks below an invitation or a
  rejection, so an employer who answers on day 65 still writes the truth over
  it automatically, with no click.
* **It refuses to run unless the mailbox is really being read.** The rule
  infers silence from the ABSENCE of inbound mail, and absence is exactly what
  a disconnected Gmail, a missing read scope or an ingestion that has never
  run also look like. Without this gate the first pass after a revoked token
  would close every open application at once — the worst false close the app
  can make, and a silent one, because a closed row stops asking for attention.

Nothing is deleted and nothing is sent: this pass only moves a status, and
every move lands in `status_history` with its own source.
"""

import asyncio
import logging

from jobdeck import db, gmail
from jobdeck import settings as app_settings
from jobdeck.constants import DEFAULT_SILENCE_CLOSES_DAYS, STATUS_NO_ANSWER

log = logging.getLogger(__name__)

SOURCE = "silence"
SETTING_DAYS = "silence_closes_after_days"

# 0 (or anything unparseable) switches the rule off rather than closing
# everything the moment the setting is cleared.
OFF = 0

# Why a pass did nothing. The screens and the log want the reason, not just
# the absence of an effect.
BLOCKED_OFF = "off"
BLOCKED_UNREAD = "mailbox-not-read"


def configured_days(con) -> int:
    """The silence window, as he set it. 0 means the rule is off."""
    return app_settings.integer(
        con, SETTING_DAYS, DEFAULT_SILENCE_CLOSES_DAYS, minimum=OFF
    )


def mailbox_is_read(con) -> bool:
    """Whether "no inbound mail" may be read as "nobody answered".

    Two things have to be true, and neither implies the other: the token must
    carry the read scope, and ingestion must have actually stored a checkpoint
    at least once. A brand-new connection with no pass behind it has no
    inbound rows for the same reason a broken one does not.
    """
    return bool(gmail.can_read()
                and db.get_setting(con, "replies_history_id", "").strip())


def _close_silent() -> dict:
    """Close every application past the window. Returns a small report."""
    closed: list[str] = []
    refused: list[str] = []
    failed: list[str] = []
    with db.db() as con:
        days = configured_days(con)
        if days == OFF:
            return {"days": OFF, "closed": [], "refused": [], "failed": [],
                    "blocked": BLOCKED_OFF}
        if not mailbox_is_read(con):
            log.info("silence: no pass — replies are not being read, so "
                     "'no answer' cannot be told from 'not looked'")
            return {"days": days, "closed": [], "refused": [], "failed": [],
                    "blocked": BLOCKED_UNREAD}
        for row in db.silent_applications(con, days):
            # `firma` is nullable in the legacy table, and this name only ever
            # reaches a log line — never let it decide whether the pass runs.
            firma = str(row["firma"] or f"Bewerbung {row['id']}")
            try:
                applied = db.set_status(
                    con, row["id"], STATUS_NO_ANSWER, SOURCE,
                    note=f"seit {str(row['last_contact'] or '')[:10]} "
                         f"ohne Antwort",
                )
                # Commit per row: one row that raises must not throw away the
                # closes that already succeeded, and each close is independent
                # and idempotent, so a partial pass is a correct pass.
                con.commit()
            except Exception:
                con.rollback()
                failed.append(firma)
                log.exception("silence: could not close application %s",
                              row["id"])
                continue
            (closed if applied else refused).append(firma)
    if closed:
        log.info("silence: closed %d application(s) after %d days: %s",
                 len(closed), days, ", ".join(sorted(closed)))
    if refused:
        # The rank guard refused the write — the row already carries a verdict
        # this pass must not overwrite. Worth a line, never an error.
        log.info("silence: left %d application(s) alone: %s",
                 len(refused), ", ".join(sorted(refused)))
    return {"days": days, "closed": closed, "refused": refused,
            "failed": failed, "blocked": ""}


async def close_silent() -> dict:
    """Run the closing pass off the event loop."""
    return await asyncio.to_thread(_close_silent)
