"""Adding a posting the user found themselves.

Discovery only ever stores what a search profile turned up, so an ad found in
a browser had no way in. Four times — 2026-08-24, 08-25, 08-26 twice, the last
eight rows in one statement — the answer was raw SQL against the live
database, which walks past every gate the product has: the cross-source
duplicate check, the company cooling-off window, and the scorer. Those rows
landed with a hand-written score, a hand-written reason, and no advert text at
all, which is the one thing a letter cannot be written without.

So this is not a second way in. It builds the same JobPosting the adapters
build and hands it to the same `polling.store_posting`, which is the only gate
there is. What it adds is the part discovery does for free and a person cannot:
fetching the text.

Two ways the text arrives:

* the URL names a posting on a source we can ask (today: the Bundesagentur,
  whose detail endpoint takes a Referenznummer) — then the source is that
  source, because that is where the ad IS. "Manual" describes how it entered,
  not where it came from, and calling it manual would cost the row its
  liveness probe and its detail refresh.
* otherwise the user pastes the text. Six of the eight urls pasted so far were
  search-results pages, three of them shared by two different postings, so a
  link is not something this can rely on.

A posting with no text is still accepted. It is worse than one with text and
the screen says so — but refusing it would send the user back to the sqlite
prompt, which is the behaviour this exists to end.
"""

import asyncio
import hashlib
import logging

import httpx

from jobdeck import dedupe
from jobdeck.constants import MANUAL_SOURCE
from jobdeck.services import polling
from jobdeck.sources import arbeitsagentur
from jobdeck.sources.base import JobPosting, extract_email, looks_remote

log = logging.getLogger(__name__)

# The same ceiling the employer-page fetch applies. A pasted advert is a whole
# web page for anyone who used Strg+A, and the scorer and the drafter both read
# this column.
MAX_TEXT = 15_000

def was_cut(text: str) -> bool:
    """True when a pasted advert was longer than what is stored.

    A cut advert reads as a COMPLETE one to the row marker, the reading pane
    and the scoring prompt — `posting_text_state` judges the stored text, and
    what was cut away is invisible to it. So the one moment it can be said is
    the moment of pasting.
    """
    return len((text or "").strip()) > MAX_TEXT


# Refusals, as reasons rather than a bare False: every one of them is shown to
# the user, and "it did not work" is what sent them to raw SQL.
NEEDS_COMPANY = "needs_company"
NEEDS_TITLE = "needs_title"


def manual_external_id(company: str, title: str) -> str:
    """A stable id for a posting that has no source id of its own.

    Derived from the same normalised company+title that `find_duplicate_job`
    compares, so re-adding the same advert meets `UNIQUE(source, external_id)`
    even if the duplicate check ahead of it were ever loosened. Hashed rather
    than slugged: a slug of a long German job title truncates, and two
    truncations that collide would file one advert as the other.
    """
    key = f"{dedupe.norm(company)}|{dedupe.norm(title)}"
    return f"manual-{hashlib.sha256(key.encode()).hexdigest()[:16]}"


def _clean(text: str) -> str:
    return (text or "").strip()


async def _from_arbeitsagentur(refnr: str, client: httpx.AsyncClient) -> JobPosting | None:
    """Fetch a BA posting by its Referenznummer, None when it yields nothing.

    A withdrawn advert answers 404 and the adapter hands the posting back
    unchanged, so "no text came" is the honest signal for both a dead ad and a
    partner listing whose text lives elsewhere — the caller falls back to what
    the user typed either way.
    """
    source = arbeitsagentur.ArbeitsagenturSource(client)
    posting = JobPosting(
        source=source.name,
        external_id=refnr,
        url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}",
    )
    try:
        posting = await source.fetch_details(posting)
    except Exception:  # noqa: BLE001 - a hand-driven action must not crash the page
        log.exception("manual: fetching %s failed", refnr)
        return None
    return posting if (posting.description or posting.title) else None


async def build(url: str, text: str, company: str, title: str, location: str,
                client: httpx.AsyncClient) -> tuple[JobPosting | None, str]:
    """Turn what the user typed into a posting, fetching the text when we can.

    Returns the posting and the reason it could not be built. Pure of the
    database on purpose: what the user must still fill in is decided before
    anything is written, so a refusal never leaves a half-stored row.
    """
    url, text, company, title, location = (
        _clean(url), _clean(text), _clean(company), _clean(title), _clean(location))

    fetched: JobPosting | None = None
    refnr = arbeitsagentur.refnr_from_url(url)
    if refnr:
        fetched = await _from_arbeitsagentur(refnr, client)

    if fetched is not None:
        # THE BOARD IS AUTHORITATIVE; what the user typed only fills what the
        # board left empty. It used to be the other way round, and the company
        # name is why that was wrong: it is the key `identity.decide`,
        # `dedupe._first_match` and the send gate all compare, so typing
        # "Beispiel" for an advert whose employer is "Beispiel GmbH" walked
        # straight past the one-application-per-company rule — the decorative-symbol
        # class, re-opened on a path where the name is TYPED rather than
        # fetched. `db.CONTACT_FIELDS` states the same rule for the other
        # hand-typed path: "the company name is the dedupe key the send gate
        # reads, and letting it be edited here would let one posting quietly
        # become a different company's."
        posting = fetched
        posting.title = posting.title or title
        posting.company = posting.company or company
        posting.location = posting.location or location
        # The text is the exception, and only when the board gave none: a
        # partner listing whose text lives on the employer's page is exactly
        # the case where he can see the advert and JobDeck cannot.
        if text and not posting.description:
            posting.description = text[:MAX_TEXT]
    elif refnr:
        # The link named a BA posting and the fetch came back empty — a
        # withdrawn advert, a partner listing with no BA-hosted text, or the
        # API refusing for three hours as it did on 2026-08-26. The posting is
        # still THAT posting, so it keeps the source and the Referenznummer:
        # filing it as `manual` would cost it its liveness probe and its detail
        # refresh, and would be the one shape that can never heal — while a BA
        # row with a Refnr picks the text up on the next pass.
        posting = JobPosting(
            source=arbeitsagentur.SOURCE_NAME,
            external_id=refnr,
            title=title,
            company=company,
            location=location,
            url=url or f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}",
            description=text[:MAX_TEXT],
            contact_email=extract_email(text),
            remote=looks_remote(title, location, text),
        )
    else:
        posting = JobPosting(
            source=MANUAL_SOURCE,
            external_id="",           # derived below, once company+title are known
            title=title,
            company=company,
            location=location,
            url=url,
            description=text[:MAX_TEXT],
            # Every adapter scans the advert for the application address, and
            # a pasted advert is an advert: without this the one posting shape
            # that DOES carry a reachable address stays `board_apply` forever,
            # because `insert_job_if_new` settles the channel from this field
            # at insert time and nothing revisits it.
            contact_email=extract_email(text),
            # the LOCATION is where he writes it: every one of the eight rows
            # he entered by hand reads "100% Remote …" there and says nothing
            # about remote in the title, so reading only title+text would drop
            # the flag on exactly the postings he is hunting for
            remote=looks_remote(title, location, text),
        )

    if not posting.company:
        return None, NEEDS_COMPANY
    if not posting.title:
        return None, NEEDS_TITLE
    if not posting.external_id:
        posting.external_id = manual_external_id(posting.company, posting.title)
    return posting, ""


async def add(url: str, text: str, company: str, title: str, location: str,
              client: httpx.AsyncClient | None = None
              ) -> tuple[polling.Stored | None, str]:
    """Add one posting the user found. Returns what storing concluded.

    The posting goes through `polling.store_posting` — the same gate discovery
    uses — so the cooling-off window and the cross-source duplicate check apply
    exactly as they do to a polled ad. `profile_id` is None: this came from no
    search profile, and the nullable column is the honest record of that.

    Nothing writes a score here. The row lands unscored, which puts it in the
    batch worker's queue and makes the list say `noch nicht bewertet` — the
    scorer's judgement, not a number typed in by the person being judged.
    """
    posting, refusal = await build(url, text, company, title, location,
                                   client or polling.http_client())
    if posting is None:
        return None, refusal
    stored = await asyncio.to_thread(polling.store_posting, None, posting)
    log.info("manual: %s %s at %s (source=%s, %d chars of text)",
             stored.outcome, posting.title, posting.company,
             posting.source, len(posting.description))
    return stored, ""
