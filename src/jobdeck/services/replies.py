"""Reply ingestion: the connected inbox, read against the register.

Every ten minutes: list what arrived, tie each message to an application
(thread → exact address → receipt → domain), classify what the German rules
can classify, write statuses through the one audited writer, mirror verdicts
as Gmail labels, and put everything the machine could not settle on the
review pile. Unmatched mail leaves ONE trace: a row holding nothing but the
opaque Gmail id — without it the pass could never advance past a capped
backlog, and with only it no content of anyone else's mail enters this
database.

The current tiering rule: a status is written automatically
only when deterministic German rules matched on a thread- or exact-address-
matched message. LLM verdicts, domain matches and everything ambiguous wait
for his click. The receipt path — the only one that WRITES an application —
additionally demands Gmail's own Authentication-Results verdict on the
sender, because a From header is what a forger controls.
"""

import asyncio
import datetime
import logging

from jobdeck import apply_channel, db, dedupe, gmail, replies
from jobdeck import settings as app_settings
from jobdeck.ai import llm
from jobdeck.ai import replies as ai_replies
from jobdeck.ai.drafting import resolve_refnr
from jobdeck.constants import (
    CLASSIFICATION_TO_STATUS,
    EMAIL_INBOUND,
    EMAIL_INBOUND_IGNORED,
    OFFENE_STATUS,
    STATUS_RANK,
)
from jobdeck.contact_resolve import registrable_domain
from jobdeck.services import apply_record

log = logging.getLogger(__name__)

# Work bounds per pass. Sixty processed messages a tick matches the channel
# resolver's cadence; the listing looks further ahead so a capped pass knows
# it has not drained. First run looks back thirty days — his unrecorded
# replies are days old, and set_status no-ops on everything already recorded.
MAX_MESSAGES_PER_PASS = 60
LIST_AHEAD = 500
FIRST_RUN_LOOKBACK_DAYS = 30

# A message whose raw form exceeds this is classified from its snippet only —
# real HR mail is kilobytes, and one huge attachment must not stall the pass.
MAX_RAW_BYTES = 5_000_000

# The receipt window and the strip's "no receipt after three days" line are
# ONE number on purpose: a receipt arriving at 60 hours must not fall between
# a closed window and a not-yet-shown notice.
RECEIPT_WINDOW_H = 72

# Gmail-side mirror of the verdicts (approved 2026-07-15), on TWO axes,
# because they answer different questions and conflating them hid the most
# important mail of his job search.
#
# WHAT IT IS — Absagen / Einladungen / Offen. 'Offen' carries everything
# that leaves the application open and undecided: a receipt, and an
# out-of-office, which answers nothing.
#
# WHETHER IT NEEDS HIM — 'Zu prüfen', carried IN ADDITION. The first version
# made these one axis, so a confident interview invitation that happened to
# match by domain was filed under "Zu prüfen" and nowhere else: on his phone
# it looked exactly like an unclear receipt, and the label that would have
# told him an invitation had arrived was the one it did not get.
LABEL_PARENT = "JobDeck"
LABEL_REVIEW = "JobDeck/Zu prüfen"
LABELS = {
    "absage": "JobDeck/Absagen",
    "einladung": "JobDeck/Einladungen",
    "eingang": "JobDeck/Offen",
    "auto": "JobDeck/Offen",
    # A label says what happened to the APPLICATION, not what kind of mail
    # arrived — and 'sonstige' leaves it open, exactly as a receipt does.
    # Without an entry here the verdict stripped every JobDeck label and
    # applied none, so the mail came out looking unread.
    "sonstige": "JobDeck/Offen",
}
# Every label this app owns; a message carries the ones that are true of it
# and none of the others, so a changed verdict cannot accumulate.
ALL_LABELS = sorted({*LABELS.values(), LABEL_REVIEW})

# How a receipt reached the ledger. The distinction decides whether an undo
# is even offered: only a row this app CREATED may be taken back out.
MATCHED_RECEIPT = "receipt"
MATCHED_ATTACHED = "receipt_known"

HISTORY_KEY = "replies_history_id"
LAST_POLL_KEY = "replies_last_poll_at"
LAST_ERROR_KEY = "replies_last_error"
AI_TOGGLE_KEY = "reply_ai_classify"
# How far a full sync reaches back. A setting rather than the constant
# because widening it is the only way to reach mail that arrived before
# JobDeck could read the mailbox at all.
LOOKBACK_KEY = "reply_lookback_days"

# Skip-style single-flight (the liveness/apply_resolve pattern): the manual
# button must learn "a pass is already running", not queue a second one.
_lock = asyncio.Lock()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _iso_from_ms(ms: int) -> str:
    if not ms:
        return ""
    moment = datetime.datetime.fromtimestamp(ms / 1000.0)
    return moment.isoformat(timespec="seconds")


def _note(key: str, value: str) -> None:
    with db.db() as con:
        db.set_setting(con, key, value)


async def ingest_replies() -> dict:
    """Scheduler entry point; also behind a manual Settings button."""
    if _lock.locked():
        return {"skipped": True}
    async with _lock:
        return await asyncio.to_thread(_ingest)


def _ingest() -> dict:
    counters = {"seen": 0, "matched": 0, "auto_status": 0, "review": 0,
                "receipts": 0, "ignored": 0, "errors": 0}
    if not gmail.can_read():
        _note(LAST_ERROR_KEY, "Gmail ohne Lese-Berechtigung — in den "
                              "Einstellungen neu verbinden")
        return {**counters, "error": "no read permission"}
    try:
        message_ids, checkpoint, from_history = _new_message_ids()
    except gmail.GmailError as exc:
        log.warning("reply ingestion: could not list the inbox: %s", exc)
        _note(LAST_ERROR_KEY, str(exc))
        return {**counters, "error": str(exc)}

    with db.db() as con:
        known = db.known_gmail_ids(con, message_ids)
    # OLDEST first. `messages.list` answers newest-first, and processing in
    # that order lets an older mail be read after a newer one — which, with
    # statuses, means an old invitation landing on top of a fresh rejection.
    # History is already chronological; reversing a list that is not sorted
    # by arrival is still the right order for the only thing that matters
    # here, which is that a later mail is never overwritten by an earlier.
    fresh = [m for m in message_ids if m not in known]
    if not from_history:
        fresh.reverse()
    drained = len(fresh) <= MAX_MESSAGES_PER_PASS
    for message_id in fresh[:MAX_MESSAGES_PER_PASS]:
        counters["seen"] += 1
        try:
            _process_message(message_id, counters)
        except Exception as exc:  # noqa: BLE001 — see below
            # ONE message must never end the pass. Catching only GmailError
            # left every other failure fatal, and the first real read proved
            # it: a second pass running concurrently had already logged a
            # message, the UNIQUE id constraint fired, and the whole run
            # died on message six of sixty. Same rule the source adapters
            # follow — malformed item, log and skip — and the checkpoint is
            # held back so the next pass retries it.
            log.warning("reply ingestion: message %s failed: %s",
                        message_id, exc)
            counters["errors"] += 1
            drained = False

    with db.db() as con:
        if drained and checkpoint:
            db.set_setting(con, HISTORY_KEY, checkpoint)
        db.set_setting(con, LAST_POLL_KEY, _now())
        # A message that fails every pass holds the checkpoint back for ever,
        # and clearing the banner unconditionally made that invisible: the
        # screen read "reading, last at 14:32" while nothing advanced. The
        # count is stated rather than the message id — the id names mail the
        # register may know nothing about.
        failures = counters["errors"]
        db.set_setting(con, LAST_ERROR_KEY, "" if not failures else (
            ("Eine Nachricht konnte nicht gelesen werden"
             if failures == 1 else
             f"{failures} Nachrichten konnten nicht gelesen werden")
            + " — der nächste Lauf versucht es erneut"))
    log.info("reply ingestion: %s", counters)
    return counters


def _lookback_days(con) -> int:
    """The full-sync window, in days, from the setting.

    Guarded: a stored value can be anything a text field accepted, and an
    unparseable one must not take the reader down — a settings page is
    reachable, a crashed scheduler job is not."""
    return app_settings.integer(
        con,
        LOOKBACK_KEY,
        FIRST_RUN_LOOKBACK_DAYS,
        minimum=1,
        maximum=3650,
        allow_decimal=True,
    )


def rescan(lookback_days: int | None = None) -> dict:
    """Re-arm the reader so it examines the mail it once skipped.

    A message no application could be found for leaves only its opaque id,
    and that id is what stops the next pass reading it again — so a skipped
    message stays skipped, and every later improvement to the matching or the
    German rules reaches only mail that has not arrived yet. This drops those
    ids and clears the incremental checkpoint, so the next passes do a full
    sync over the lookback window and judge them afresh.

    Messages already tied to an application are untouched: their rows stay,
    so the duplicate gate still refuses to file them twice. Nothing is read
    or written here — the passes that follow do the work, at their own bounded
    rate.
    """
    with db.db() as con:
        if lookback_days is not None:
            db.set_setting(con, LOOKBACK_KEY, str(max(int(lookback_days), 1)))
        forgotten = db.forget_ignored_messages(con)
        db.set_setting(con, HISTORY_KEY, "")
        db.set_setting(con, LAST_ERROR_KEY, "")
        days = _lookback_days(con)
    log.info("reply ingestion: re-armed — %d skipped message(s) forgotten, "
             "lookback %d days", forgotten, days)
    return {"forgotten": forgotten, "lookback_days": days}


def _new_message_ids() -> tuple[list[str], str, bool]:
    """(candidate ids, checkpoint to store once drained, came-from-history).

    The third value says whether the ids arrived in arrival order: history
    records are chronological, a search answers newest-first."""
    with db.db() as con:
        stored = db.get_setting(con, HISTORY_KEY, "")
    if stored:
        try:
            ids, checkpoint = gmail.history_added_messages(stored, LIST_AHEAD)
            return ids, checkpoint, True
        except gmail.GmailHistoryExpired:
            log.info("reply ingestion: history checkpoint expired — "
                     "re-baselining with a full sync")
    # Full sync: the checkpoint is taken BEFORE listing, so anything arriving
    # while this pass runs is covered by the next incremental read.
    checkpoint = gmail.profile_history_id()
    with db.db() as con:
        days = _lookback_days(con)
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    query = f"-from:me after:{int(cutoff.timestamp())}"
    return gmail.list_new_message_ids(query, LIST_AHEAD), checkpoint, False


def _ignore(message_id: str) -> None:
    """The whole trace an unmatched message leaves: its opaque Gmail id.

    Not even the arrival time — that is a fact about somebody else's mail,
    and the id alone does the one job this row exists for, which is letting
    a bounded pass advance past a backlog without reading the same message
    for ever."""
    with db.db() as con:
        db.add_email_log(con, {
            "direction": EMAIL_INBOUND_IGNORED,
            "gmail_message_id": message_id,
        })


def _process_message(message_id: str, counters: dict) -> None:
    meta = gmail.get_message_metadata(message_id)
    headers = meta["headers"]
    from_addr = replies.from_address(headers.get("from", ""))
    subject = headers.get("subject", "")

    with db.db() as con:
        own_address = db.get_setting(con, "gmail_address", "").strip().lower()
    if not from_addr or (own_address and from_addr == own_address):
        counters["ignored"] += 1
        _ignore(message_id)
        return

    match = _match(meta, from_addr, subject)
    if match is None:
        counters["ignored"] += 1
        _ignore(message_id)
        return
    counters["matched"] += 1

    body = _body_for(message_id, meta)
    if match["kind"] == "receipt":
        _handle_receipt(match, meta, from_addr, subject, body, counters)
    else:
        _handle_reply(match, meta, from_addr, subject, body, counters)


def _body_for(message_id: str, meta: dict) -> str:
    """The readable text of a MATCHED message; '' degrades to snippet-only.

    Bodies are fetched only here — after the cascade matched — so the mail
    of everyone else never leaves Gmail. A fetch or parse failure costs this
    row its body, never the pass: the rules then stay silent and the row
    lands on the review pile with the snippet."""
    if meta["size_estimate"] > MAX_RAW_BYTES:
        log.info("reply ingestion: message %s too large (%d bytes) — "
                 "classifying from the snippet", message_id,
                 meta["size_estimate"])
        return ""
    try:
        return replies.extract_text(gmail.get_message_raw(message_id))
    except gmail.GmailError as exc:
        log.warning("reply ingestion: could not fetch the body of %s: %s",
                    message_id, exc)
        return ""


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
def _match(meta: dict, from_addr: str, subject: str) -> dict | None:
    """Thread → exact address → receipt → domain; first hit wins.

    The receipt arms run before the domain arm on purpose: an ATS
    confirmation arrives from the vendor's domain, and a vendor domain must
    never DOMAIN-match some unrelated application whose contact happens to
    sit at the same registrable name."""
    with db.db() as con:
        bewerbung_id = db.find_bewerbung_by_thread(con, meta["thread_id"])
        if bewerbung_id is not None:
            return {"kind": "reply", "bewerbung_id": bewerbung_id,
                    "matched_by": "thread"}
        rows = db.bewerbungen_for_reply_match(con)
        by_address = [row for row in rows
                      if str(row["email"] or "").strip().lower() == from_addr]
        if by_address:
            # One address can hold several applications over time (an
            # agency, a large employer). Prefer the ones still waiting for
            # an answer; if that is still not a single row, the mail names
            # no application and he decides which.
            open_rows = [row for row in by_address
                         if str(row["status"] or "") in OFFENE_STATUS]
            candidates = open_rows or by_address
            return {"kind": "reply", "bewerbung_id": int(candidates[0]["id"]),
                    "matched_by": "address",
                    "ambiguous": len(candidates) > 1}
        receipt = _receipt_match(con, meta, from_addr, subject)
        if receipt is not None:
            return receipt
        sender_domain = replies.matchable_domain(from_addr)
        if sender_domain:
            hits = {int(row["id"]) for row in rows
                    if replies.matchable_domain(str(row["email"] or ""))
                    == sender_domain}
            if len(hits) == 1:
                return {"kind": "reply", "bewerbung_id": hits.pop(),
                        "matched_by": "domain"}
        named = _name_match(con, meta, from_addr)
        if named is not None:
            return named
    return None


def _name_match(con, meta: dict, from_addr: str) -> dict | None:
    """Last arm: does the sender look like a company he applied to?

    This is the ONLY arm that reaches a form application. Those carry no
    address — 29 of his 55 open applications — so every arm above is
    structurally blind to them, and a reply to one was unmatchable by
    construction rather than by accident.

    It never writes a status: 'name' is not in the tier _handle_reply may
    file automatically, so the strongest thing this can do is put the mail
    in front of him with a company already suggested. Ambiguity is refused
    outright — two applications answering to one name is exactly the case
    where a guess would be worse than the question.
    """
    from_header = str(meta["headers"].get("from", ""))
    rows = db.bewerbungen_for_name_match(con)
    hits = [row for row in rows
            if replies.company_in_sender(str(row["firma"] or ""),
                                         from_header, from_addr)]
    if not hits:
        return None
    # An employer writes about the application that is still open; a settled
    # one is history. Same preference the address arm makes.
    open_rows = [row for row in hits
                 if str(row["status"] or "") in OFFENE_STATUS]
    candidates = open_rows or hits
    if len(candidates) != 1:
        return None
    return {"kind": "reply", "bewerbung_id": int(candidates[0]["id"]),
            "matched_by": "name"}


def _receipt_match(con, meta: dict, from_addr: str, subject: str) -> dict | None:
    """An Eingangsbestätigung against the Läuft strip — 1-3 rows, never the
    corpus. Strong evidence identifies ONE posting; anything ambiguous or
    name-only becomes a proposal for his click."""
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(hours=RECEIPT_WINDOW_H)
              ).isoformat(timespec="seconds")
    candidates = db.receipt_candidates(con, cutoff)
    if not candidates:
        return None
    if replies.is_bulk_mailing(meta["headers"]):
        # A mailing list is not a receipt. Real ATS confirmations do
        # occasionally carry an unsubscribe footer, so this bars only the
        # RECEIPT arm — the one that writes an application — and leaves a
        # reply to an application it can already identify alone.
        return None
    sender_domain = registrable_domain(from_addr.rpartition("@")[2])
    # The body is not fetched yet at match time; strong textual evidence is
    # judged on the subject plus Gmail's snippet, which carries the opening
    # lines where ATS mail states the reference.
    text_window = f"{subject}\n{meta['snippet']}"
    identified: list[tuple[dict, str, bool]] = []
    weak: list[dict] = []
    for job in candidates:
        evidence, authorizing = _receipt_evidence(job, sender_domain,
                                                  text_window)
        if evidence:
            identified.append((dict(job), evidence, authorizing))
        elif _company_named(job, from_addr, meta["headers"].get("from", "")):
            weak.append(dict(job))
    if len(identified) == 1:
        job, evidence, authorizing = identified[0]
        authenticated = replies.sender_authenticated(meta["headers"])
        strong = authorizing and authenticated
        if not authorizing:
            evidence += " · Absender gehört nicht zur Anzeige"
        elif not authenticated:
            evidence += " · Absender nicht verifiziert"
        return {"kind": "receipt", "job": job, "strong": strong,
                "evidence": evidence}
    if len(identified) > 1:
        # Two forms at the same ATS inside the window: the evidence names a
        # vendor, not a posting. Propose, never write.
        return {"kind": "receipt", "job": identified[0][0], "strong": False,
                "evidence": "mehrere laufende Formulare passen"}
    if len(weak) == 1:
        return {"kind": "receipt", "job": weak[0], "strong": False,
                "evidence": "Firmenname"}
    return None


def _receipt_evidence(job, sender_domain: str, text: str) -> tuple[str, bool]:
    """(what identified this posting, may it AUTHORIZE a ledger write).

    Only the sender's own domain can authorize. A Referenznummer is printed
    in the public advert, so anyone who read the posting can quote it — it
    identifies which posting a mail is about, and says nothing about who
    sent it. Refnr therefore corroborates an aligned sender and otherwise
    yields a proposal for his click.

    No domain a job board lives at can be a target, whichever column holds
    it — `jobs.url` is the board's own page, and `apply_url` is the board's
    link on any posting found through one. A board writes to everybody who
    ever touched it, so letting one authorize would let a newsletter or a
    notification record an application at an employer that never wrote.
    """
    refnr = resolve_refnr(job)
    by_refnr = replies.refnr_in_text(refnr, text, "")
    if sender_domain:
        # A board is not an employer, so no domain a board lives at may
        # authorize — whichever column offered it. Asking the row's CHANNEL
        # instead was the hole: `apply_channel` is a derivation that MOVES,
        # and entering a contact address by hand moves it to direct_email
        # while `apply_url` keeps the board link the posting was found by.
        # A board's own mail then aligned with the posting and filed itself
        # as an Eingangsbestätigung, which writes a status.
        targets = {
            domain
            for domain in (
                registrable_domain(str(job["apply_url"] or "")),
                registrable_domain(
                    str(job["contact_email"] or "").rpartition("@")[2]),
            )
            if domain and not apply_channel.is_board_domain(domain)
        }
        if sender_domain in targets:
            evidence = f"Absender {sender_domain}"
            return (f"{evidence} · Refnr {refnr}" if by_refnr else evidence), True
        vendor = str(job["ats_vendor"] or "")
        if vendor:
            sender_channel = apply_channel.classify(f"https://{sender_domain}/")
            if (sender_channel.channel == apply_channel.CHANNEL_ATS
                    and sender_channel.vendor == vendor):
                evidence = f"ATS {vendor}"
                return (f"{evidence} · Refnr {refnr}" if by_refnr
                        else evidence), True
    if by_refnr:
        return f"Refnr {refnr}", False
    return "", False


def _company_named(job, from_addr: str, from_header: str) -> bool:
    """Weak evidence: the posting's company named in the sender.

    Conservative on purpose — company names collide and a newsletter is not
    a receipt — and only ever yields a PROPOSAL."""
    company = dedupe.fold(str(job["company"] or ""))
    if len(company) < 4:
        return False
    sender = dedupe.fold(f"{replies.from_display_name(from_header)} "
                         f"{from_addr}")
    return company in sender


def _verdict_for(subject: str, body: str) -> replies.RuleVerdict | None:
    """The rules' reading of a message, with the no-body case honest.

    A body can be missing three ways: too large to fetch, a transport
    failure, or a mail that says everything in its header. All three used to
    throw the SUBJECT away as well, so such a message could only ever reach
    the review pile unlabelled — "Absage zu Ihrer Bewerbung" included, which
    is as plain as German HR mail gets.

    It is read now, and it proposes: one line is thinner evidence than a
    letter, and the conditional screen has no sentence to work on. An
    out-of-office is exempt because its own marker IS the subject.
    """
    verdict = replies.classify(subject, body)
    if (verdict is None or body
            or verdict.classification == replies.CLASS_AUTO):
        return verdict
    return replies.RuleVerdict(verdict.classification, verdict.pattern,
                               confident=False)


# --------------------------------------------------------------------------
# Replies to sent applications
# --------------------------------------------------------------------------
def _handle_reply(match: dict, meta: dict, from_addr: str, subject: str,
                  body: str, counters: dict) -> None:
    verdict = _verdict_for(subject, body)
    classification = ""
    classified_by = ""
    needs_review = 1
    note = ""
    if verdict is not None:
        classification = verdict.classification
        classified_by = "rules"
        note = verdict.pattern
        if classification == replies.CLASS_AUTO:
            needs_review = 0  # a machine answer answers nothing — ledger only
        elif (verdict.confident
              and not match.get("ambiguous")
              and not replies.is_bulk_mailing(meta["headers"])
              and match["matched_by"] in ("thread", "address")
              and (match["matched_by"] == "thread"
                   or replies.sender_authenticated(meta["headers"]))):
            # Four conditions, each earned: the rules must not have leaned
            # on the conditional screen (a gap there is a silent wrong
            # status), the mail must not be addressed to a list, it must be
            # tied to the application by more than a domain, and — on the
            # address arm — Gmail itself must vouch for the sender, because
            # a From header is what a forger writes. A thread id is not
            # forgeable: only mail Gmail itself threaded into a message this
            # app sent carries one.
            #
            # The list screen is here because an HR mailbox sends both kinds
            # of mail. A Bewerberpool round-robin from the very address he
            # corresponded with trips "keine passende Stelle anbieten" and
            # would close a live application without a word — and once that
            # rank-4 Absage is filed, the real invitation behind it can no
            # longer overwrite it. A mass mail is by construction not an
            # answer to HIS application: nobody read his file before sending
            # it. It still gets classified and filed; it just waits for him.
            needs_review = 0
    elif replies.is_auto_submitted(meta["headers"]):
        classification = replies.CLASS_AUTO
        classified_by = "rules"
        note = "Auto-Submitted/Bulk-Header"
        needs_review = 0
    elif body and _ai_classify_enabled():
        classification, note, _usage = _ai_classify(subject, body)
        if classification:
            classified_by = "llm"

    with db.db() as con:
        email_log_id = db.add_email_log(con, {
            "direction": EMAIL_INBOUND,
            "gmail_message_id": meta["id"],
            "gmail_thread_id": meta["thread_id"],
            "from_addr": from_addr,
            "subject": subject,
            "snippet": meta["snippet"][:120],
            "internal_date": _iso_from_ms(meta["internal_date_ms"]),
            "bewerbung_id": match["bewerbung_id"],
            "matched_by": match["matched_by"],
            "classification": classification,
            "classified_by": classified_by,
            "needs_review": needs_review,
            "body_text": body,
            "matched_note": note,
        })
        status = CLASSIFICATION_TO_STATUS.get(classification)
        if needs_review == 0 and classified_by == "rules" and status:
            if db.set_status(con, match["bewerbung_id"], status,
                             source="reply_auto", email_log_id=email_log_id,
                             note=note):
                counters["auto_status"] += 1
            else:
                # The anti-downgrade rank refused it: the application is
                # already settled, and no automatic source may move a
                # settled verdict sideways. Left filed, this row would sit
                # in the ledger reading "Absage · automatisch" beside an
                # application that says Einladung. It is HIS to reconcile.
                needs_review = 1
                db.classify_reply_row(con, email_log_id, classification,
                                      classified_by, 1)
    if needs_review:
        counters["review"] += 1
    # Labelled either way: a mail waiting for him is the one he most needs
    # to find in Gmail.
    _apply_label(meta["id"], classification, bool(needs_review))


def _ai_classify_enabled() -> bool:
    """The double gate, verbatim from contact_lookup: the master switch's
    promise is that nothing is sent to the API while it is off."""
    with db.db() as con:
        return (db.ai_enabled(con)
                and app_settings.boolean(con, AI_TOGGLE_KEY, False))


def _ai_classify(subject: str, body: str) -> tuple[str, str, object]:
    try:
        classification, begruendung, usage = ai_replies.classify_reply(
            subject, body)
    except llm.LLMNotConfigured:
        return "", "", None
    except llm.LLMError as exc:
        if exc.usage is not None:  # the failed call still cost tokens
            _record_usage(exc.usage)
        log.warning("reply ingestion: LLM classification failed: %s", exc)
        return "", "", None
    _record_usage(usage)
    return classification, begruendung, usage


def _record_usage(usage) -> None:
    with db.db() as con:
        db.record_llm_usage(con, usage.input_tokens, usage.output_tokens,
                            usage.cost_usd)


# --------------------------------------------------------------------------
# Receipts (Eingangsbestätigungen) against the Läuft strip
# --------------------------------------------------------------------------
def _handle_receipt(match: dict, meta: dict, from_addr: str, subject: str,
                    body: str, counters: dict) -> None:
    job = match["job"]
    # What the mail SAYS, not merely who sent it. Matching identifies which
    # posting a mail belongs to; it is no evidence that the mail is a
    # receipt — a fast rejection arrives from exactly the same ATS domain,
    # and filing it as "your application arrived" would state the opposite
    # of what the employer wrote.
    verdict = _verdict_for(subject, body)
    said = verdict.classification if verdict is not None else ""
    row = {
        "direction": EMAIL_INBOUND,
        "gmail_message_id": meta["id"],
        "gmail_thread_id": meta["thread_id"],
        "from_addr": from_addr,
        "subject": subject,
        "snippet": meta["snippet"][:120],
        "internal_date": _iso_from_ms(meta["internal_date_ms"]),
        "body_text": body,
        "job_id": int(job["id"]),
        "matched_by": MATCHED_RECEIPT,
        "classification": said or "eingang",
        "classified_by": "rules",
        # What identified this posting, so a proposal can say why it is only
        # a proposal instead of leaving him to guess.
        "matched_note": match.get("evidence", ""),
    }
    if said and said != "eingang":
        # An answer, not a receipt. There is no application in the ledger to
        # carry it yet, so recording one AND filing its outcome is two
        # decisions at once — he makes them with one press.
        _propose(row, counters, meta)
        return
    if not match["strong"]:
        _propose(row, counters, meta)
        return

    bewerbung_id = int(job["bewerbung_id"] or 0) or None
    if bewerbung_id is not None:
        # The healing arm: the application is already in the ledger — he
        # pressed „Abgeschickt" himself, or a crash landed between the two
        # writes. This mail only ATTACHES to it, so it must never offer an
        # undo: taking it back would delete a row this app did not create.
        row["matched_by"] = MATCHED_ATTACHED
    if bewerbung_id is None:
        # The one automatic ledger write in this app, through the one
        # recorder — the strip pressed by the receipt instead of by him.
        outcome = apply_record.record_form_application(
            int(job["id"]), source="eingang")
        if not outcome["ok"]:
            # The duplicate gate refused inside the write: the receipt
            # becomes a review row instead of a ledger row.
            _propose(row, counters, meta)
            return
        bewerbung_id = outcome["bewerbung_id"]
    row["bewerbung_id"] = bewerbung_id
    with db.db() as con:
        email_log_id = db.add_email_log(con, row)
        db.set_status(con, bewerbung_id, "In Bearbeitung",
                      source="reply_auto", email_log_id=email_log_id,
                      note=f"Eingangsbestätigung ({match['evidence']}) · "
                           f"Gmail {meta['id']}")
    counters["receipts"] += 1
    _apply_label(meta["id"], "eingang")


# --------------------------------------------------------------------------
# Labels — best effort, never the pass's problem
# --------------------------------------------------------------------------
def _propose(row: dict, counters: dict, meta: dict) -> None:
    """Park a receipt on the review pile — and mark it in Gmail as waiting.

    One writer for the three paths that reach this, so a new one cannot
    forget the label the way the first version forgot it everywhere."""
    row["needs_review"] = 1
    with db.db() as con:
        db.add_email_log(con, row)
    counters["review"] += 1
    _apply_label(meta["id"], row.get("classification", ""), needs_review=True)


def _apply_label(message_id: str, classification: str,
                 needs_review: bool = False) -> None:
    """Make the message carry exactly the JobDeck labels that are true of it.

    Two axes, not one. WHAT IT IS goes on whenever the rules read it —
    a confident invitation is an invitation whether or not the app was
    allowed to write the status — and 'Zu prüfen' goes on IN ADDITION when
    it needs him. Carrying only one hid his most important mail: an
    interview invitation matched by domain got 'Zu prüfen' and nothing
    else, so in Gmail it was indistinguishable from an unclear receipt.

    Everything else this app owns is removed, so a corrected verdict cannot
    leave its old label behind."""
    if not message_id:
        return
    names = []
    verdict_label = LABELS.get(classification)
    if verdict_label:
        names.append(verdict_label)
    if needs_review:
        names.append(LABEL_REVIEW)
    try:
        resolved = gmail.ensure_labels([LABEL_PARENT, *ALL_LABELS])
        keep = [resolved[name] for name in names]
        drop = [resolved[other] for other in ALL_LABELS if other not in names]
        gmail.set_labels(message_id, keep, drop)
    except (gmail.GmailError, KeyError) as exc:
        log.warning("reply ingestion: could not label %s: %s",
                    message_id, exc)


# --------------------------------------------------------------------------
# Review actions (the /antworten page's handlers, sync — call via io_bound)
# --------------------------------------------------------------------------
def resolve_review(email_log_id: int, classification: str,
                   force_status: bool = False) -> dict:
    """His verdict on a review row: classify, label, and apply the status —
    unless applying it would take the application BACKWARDS.

    Measured on his real shelf: 23 of 42 waiting mails hang off applications
    already standing at 'Absage', 8 of them proposing 'Eingang'. Because this
    writes with source='reply_manual' — exempt from the anti-downgrade rank so
    that his correction can win — one ordinary press would silently reopen an
    application he closed himself.

    So the FIRST press files the mail and keeps the status, and the screen
    offers a second, explicit press (`force_status=True`) that writes it. The
    rule is the one `set_status` already applies to automatic sources: a press
    may RAISE a status, never lower it or move it sideways. A verdict that
    raises — 34 of his 42 — behaves exactly as before.

    Returns what happened, so the screen can say it: `status_written` False
    with `kept` and `would_be` set means the mail is filed and the register
    was left alone."""
    if classification not in CLASSIFICATION_TO_STATUS:
        return {"ok": False, "status_written": False}
    wanted = CLASSIFICATION_TO_STATUS[classification]
    with db.db() as con:
        row = db.get_email_log(con, email_log_id)
        if row is None:
            return {"ok": False, "status_written": False}
        # The mail is read either way: what it IS does not depend on whether
        # the register may move. Leaving it on the shelf would ask him the
        # same question again tomorrow.
        db.classify_reply_row(con, email_log_id, classification,
                              "reply_manual", 0)
        message_id = str(row["gmail_message_id"] or "")
        result = {"ok": True, "status_written": False, "kept": "",
                  "would_be": wanted}
        if row["bewerbung_id"] is not None:
            bewerbung = db.get_bewerbung(con, int(row["bewerbung_id"]))
            current = str(bewerbung["status"] or "") if bewerbung else ""
            if (not force_status and current != wanted
                    and STATUS_RANK.get(wanted, 0)
                    <= STATUS_RANK.get(current, 0)):
                result["kept"] = current
            else:
                result["status_written"] = db.set_status(
                    con, int(row["bewerbung_id"]), wanted,
                    source="reply_manual", email_log_id=email_log_id)
    if message_id:
        _apply_label(message_id, classification, needs_review=False)
    return result


def dismiss_review(email_log_id: int) -> None:
    """'This mail does not belong to that application' — unlink and settle.

    The Gmail label goes with it: a mail he pushed aside must not keep
    telling him from his phone that something is waiting."""
    with db.db() as con:
        row = db.get_email_log(con, email_log_id)
        db.link_reply_bewerbung(con, email_log_id, None)
        db.classify_reply_row(con, email_log_id, "", "", 0)
        message_id = str(row["gmail_message_id"] or "") if row else ""
    if message_id:
        _apply_label(message_id, "", needs_review=False)


def reopen_review(email_log_id: int) -> bool:
    """Put a dismissed mail back on the shelf.

    `dismiss_review` keeps the row — it only unlinks and settles it — so the
    press was a one-way door with no schema reason to be one. The mail carries
    its waiting label again, because it really is waiting again."""
    with db.db() as con:
        row = db.get_email_log(con, email_log_id)
        if row is None or str(row["direction"]) != EMAIL_INBOUND:
            return False
        db.reopen_reply_review(con, email_log_id)
        message_id = str(row["gmail_message_id"] or "")
        classification = str(row["classification"] or "")
    if message_id:
        _apply_label(message_id, classification, needs_review=True)
    return True


def dismiss_many(email_log_ids: list[int]) -> int:
    """File a whole view of waiting mail WITHOUT writing a single status.

    The one bulk gesture on the screen, and deliberately not a bulk verdict:
    `resolve_review` writes with source='reply_manual', which the
    anti-downgrade rank exempts, so "confirm all twelve" would be twelve
    unguarded status writes in one press. This one only says "these mails
    need nothing from me", which is exactly true of mail arriving for an
    application that is already closed."""
    for email_log_id in email_log_ids:
        # No defensive cast: the ids come from the rows this page just drew,
        # so a bad one is a bug and has to be seen rather than counted as a
        # filed mail. `dismiss_review` is a no-op on an id that is already
        # gone, which is the only race worth surviving here.
        dismiss_review(email_log_id)
    return len(email_log_ids)


def adopt_receipt(email_log_id: int) -> dict:
    """One press on a weak receipt proposal: record the application."""
    with db.db() as con:
        row = db.get_email_log(con, email_log_id)
    if row is None or row["job_id"] is None:
        return {"ok": False}
    with db.db() as con:
        job = db.get_job(con, int(row["job_id"]))
    if job is not None and job["bewerbung_id"] is not None:
        # It was recorded meanwhile — by him, or by an earlier pass. Record
        # it again and `apply_job` marks the posting a DUPLICATE of its own
        # application; attach instead, which is what the press meant.
        bewerbung_id = int(job["bewerbung_id"])
        with db.db() as con:
            bewerbung = db.get_bewerbung(con, bewerbung_id)
            current = str(bewerbung["status"] or "") if bewerbung else ""
            db.link_reply_bewerbung(con, email_log_id, bewerbung_id)
            # The same restatement the ingestion arm makes when it finds the
            # application already there. Without it the row keeps claiming
            # this app created the ledger row, `undo_receipt` accepts, and
            # the undo deletes an application HE recorded by hand.
            db.set_reply_matched_by(con, email_log_id, MATCHED_ATTACHED)
            db.classify_reply_row(con, email_log_id, "eingang",
                                  "reply_manual", 0)
            # The same rule the verdict buttons follow: a receipt is rank 2
            # and may RAISE a status, never reopen one he has already closed.
            # Without this, "Als Bewerbung eintragen" was a way around the
            # guard on the very screen that states it.
            if STATUS_RANK.get("In Bearbeitung", 0) > STATUS_RANK.get(current, 0):
                db.set_status(con, bewerbung_id, "In Bearbeitung",
                              source="reply_manual", email_log_id=email_log_id)
        _apply_label(str(row["gmail_message_id"] or ""), "eingang")
        return {"ok": True, "bewerbung_id": bewerbung_id,
                "company": str(job["company"] or ""), "duplicate": None,
                "undo": False}
    outcome = apply_record.record_form_application(
        int(row["job_id"]), source="eingang")
    if not outcome["ok"]:
        return outcome
    with db.db() as con:
        db.link_reply_bewerbung(con, email_log_id, outcome["bewerbung_id"])
        db.classify_reply_row(con, email_log_id, "eingang", "reply_manual", 0)
        db.set_status(con, outcome["bewerbung_id"], "In Bearbeitung",
                      source="reply_manual", email_log_id=email_log_id)
    message_id = str(row["gmail_message_id"] or "")
    if message_id:
        _apply_label(message_id, "eingang")
    return outcome


def undo_receipt(email_log_id: int) -> bool:
    """Take a receipt-recorded application back out — real work, not a flag:
    the ledger row goes, the posting returns, and the mail returns to the
    review pile where it can be re-adopted or dismissed.

    Refused unless THIS app created that ledger row (`matched_by` is the
    receipt arm, not the attach arm). The healing arm attaches a receipt to
    an application he recorded by hand, and undoing there would delete a row
    the reader never wrote — the ledger is not the reader's to destroy.

    previous_status is 'new' by construction: receipt candidates are strip
    rows, and since v10 an opened form leaves `jobs.status` untouched."""
    with db.db() as con:
        row = db.get_email_log(con, email_log_id)
    if row is None or row["job_id"] is None or row["bewerbung_id"] is None:
        return False
    if str(row["matched_by"] or "") != MATCHED_RECEIPT:
        return False
    apply_record.undo(int(row["job_id"]), int(row["bewerbung_id"]), "new")
    with db.db() as con:
        # apply_record.undo cleared email_log.bewerbung_id already
        db.classify_reply_row(con, email_log_id, "eingang", "rules", 1)
    # The mail really is waiting again, so Gmail has to say so again —
    # otherwise his phone shows a settled mail while the shelf shows one
    # asking for him.
    _apply_label(str(row["gmail_message_id"] or ""), "eingang",
                 needs_review=True)
    return True
