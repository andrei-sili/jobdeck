"""Adding a posting the user found themselves.

The point of the slice is that this is NOT a second way in: whatever the user
pastes goes through the same gate discovery uses. So most of what these pin is
that the gates still fire — a hand-added row must not be able to do what raw
SQL did four times.
"""

import httpx
import pytest

from jobdeck import attempts, dates, db, identity
from jobdeck.services import manual_posting as mp
from jobdeck.services import polling
from jobdeck.sources.base import JobPosting


@pytest.fixture()
def client():
    return httpx.AsyncClient()


def _detail_payload(**over):
    payload = {
        "stellenangebotsTitel": "Junior Python-Entwickler (m/w/d)",
        "stellenangebotsBeschreibung": "Wir suchen eine Junior-Entwicklerin. " * 40,
        "firma": "Beispiel GmbH",
    }
    payload.update(over)
    return payload


class _Detail:
    """Stands in for the BA source: fetch_details is the only method used."""

    name = "arbeitsagentur"

    def __init__(self, payload=None):
        self._payload = payload

    async def fetch_details(self, posting: JobPosting) -> JobPosting:
        if self._payload is None:  # a withdrawn ad: the adapter hands it back
            return posting
        posting.title = self._payload.get("stellenangebotsTitel", "")
        posting.company = self._payload.get("firma", "")
        posting.description = self._payload.get("stellenangebotsBeschreibung", "")
        return posting


# --------------------------------------------------------------- building


async def test_a_pasted_advert_becomes_a_posting(client):
    posting, refusal = await mp.build(
        url="", text="Wir suchen dich fuer Django und PostgreSQL.",
        company="Beispiel GmbH", title="Junior Backend", location="Remote",
        client=client)
    assert refusal == ""
    assert posting.source == mp.MANUAL_SOURCE
    assert posting.company == "Beispiel GmbH"
    assert posting.title == "Junior Backend"
    assert "Django" in posting.description


async def test_a_bundesagentur_link_fetches_the_text_itself(client, monkeypatch):
    """The whole reason a URL field exists: on a source we can ask, the user
    should not have to copy an advert JobDeck can fetch."""
    monkeypatch.setattr(mp.arbeitsagentur, "ArbeitsagenturSource",
                        lambda c: _Detail(_detail_payload()))
    posting, refusal = await mp.build(
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003535918-S",
        text="", company="", title="", location="", client=client)
    assert refusal == ""
    # the source is the source the ad IS on — calling it 'manual' would cost
    # the row its liveness probe and its detail refresh
    assert posting.source == "arbeitsagentur"
    assert posting.external_id == "10001-1003535918-S"
    assert posting.company == "Beispiel GmbH"
    assert len(posting.description) > 500


async def test_a_bare_link_needs_nothing_typed_at_all(monkeypatch):
    """Driven through the REAL adapter over a mock transport, not the stub above.

    This is the whole reason the URL field exists, and the stub could not see
    it: the stub sets a title, the real `fetch_details` had to be taught to.
    Against the live API the path was refused as `needs_title` while the entire
    suite was green.
    """
    payload = _detail_payload()

    def handler(request):
        assert "jobdetails" in str(request.url)
        return httpx.Response(200, json=payload)

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    posting, refusal = await mp.build(
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003535918-S",
        text="", company="", title="", location="", client=transport)
    assert refusal == ""
    assert posting.title == payload["stellenangebotsTitel"]
    assert posting.company == payload["firma"]
    assert posting.description == payload["stellenangebotsBeschreibung"]
    assert posting.source == "arbeitsagentur"


async def test_the_board_is_authoritative_and_typing_only_fills_gaps(
        client, monkeypatch):
    """The company name is the key `identity.decide`, `dedupe._first_match` and
    the send gate all compare. Letting a typed value override the board's meant
    typing "Beispiel" for an advert whose employer is "Beispiel GmbH" walked
    straight past the one-application-per-company rule — the decorative-symbol
    class,
    re-opened where the name is TYPED rather than fetched. `db.CONTACT_FIELDS`
    states the same rule for the other hand-typed path."""
    monkeypatch.setattr(mp.arbeitsagentur, "ArbeitsagenturSource",
                        lambda c: _Detail(_detail_payload()))
    posting, _ = await mp.build(
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003535918-S",
        text="", company="Andere GmbH", title="Anderer Titel",
        location="Kiel", client=client)
    assert posting.company == "Beispiel GmbH"          # the board's, not his
    assert posting.title == "Junior Python-Entwickler (m/w/d)"


async def test_typing_fills_only_what_the_board_left_empty(client, monkeypatch):
    """The other half of the same rule: a payload that states no employer must
    still be able to take the one he is reading off the page."""
    monkeypatch.setattr(
        mp.arbeitsagentur, "ArbeitsagenturSource",
        lambda c: _Detail(_detail_payload(firma="", stellenangebotsTitel="")))
    posting, refusal = await mp.build(
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003535918-S",
        text="", company="Beispiel GmbH", title="Junior Backend",
        location="Kiel", client=client)
    assert refusal == ""
    assert posting.company == "Beispiel GmbH"
    assert posting.title == "Junior Backend"
    assert posting.location == "Kiel"


async def test_a_pasted_text_never_replaces_the_boards_own(client, monkeypatch):
    """The advert JobDeck fetched is the advert. A paste can only supply one
    the board did not give — a partner listing whose text lives elsewhere."""
    monkeypatch.setattr(mp.arbeitsagentur, "ArbeitsagenturSource",
                        lambda c: _Detail(_detail_payload()))
    posting, _ = await mp.build(
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003535918-S",
        text="Etwas ganz anderes.", company="", title="", location="",
        client=client)
    assert posting.description.startswith("Wir suchen eine Junior-Entwicklerin")


async def test_a_paste_supplies_the_text_a_partner_listing_lacks(
        client, monkeypatch):
    monkeypatch.setattr(
        mp.arbeitsagentur, "ArbeitsagenturSource",
        lambda c: _Detail(_detail_payload(stellenangebotsBeschreibung="")))
    posting, _ = await mp.build(
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003535918-S",
        text="Der Text von der Arbeitgeberseite.", company="", title="",
        location="", client=client)
    assert posting.description == "Der Text von der Arbeitgeberseite."


async def test_a_withdrawn_advert_keeps_its_identity_as_a_ba_posting(
        client, monkeypatch):
    """A 404 hands the posting back unchanged, so the link yields nothing — but
    the advert is still THAT advert. Filing it as `manual` would cost it its
    liveness probe and its detail refresh, and would be the one shape that can
    never heal; a BA row with a Referenznummer picks the text up on the next
    pass. On 2026-08-26 the API refused everything for three hours, so "the
    fetch came back empty" is not the same as "this is not a BA posting"."""
    monkeypatch.setattr(mp.arbeitsagentur, "ArbeitsagenturSource",
                        lambda c: _Detail(None))
    posting, refusal = await mp.build(
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003535918-S",
        text="Der Anzeigentext, von Hand kopiert.",
        company="Beispiel GmbH", title="Junior Backend", location="",
        client=client)
    assert refusal == ""
    assert posting.source == "arbeitsagentur"
    assert posting.external_id == "10001-1003535918-S"
    assert posting.description == "Der Anzeigentext, von Hand kopiert."
    assert posting.url.endswith("/10001-1003535918-S")


@pytest.mark.parametrize("url", [
    # a BA SEARCH page, on the agency's own host — the case the test name
    # claims. The first version used a jooble URL, which only proved that a
    # non-BA link is not fetched.
    "https://www.arbeitsagentur.de/jobsuche/suche?was=Python",
    "https://www.arbeitsagentur.de/jobsuche/suche/ergebnisliste/lang",
    # and another board entirely
    "https://de.jooble.org/stellenangebote-junior-softwareentwickler-remote",
])
async def test_a_search_page_is_not_treated_as_a_posting(
        client, monkeypatch, url):
    """Six of the eight urls pasted by hand were search pages. Fetching one
    would store somebody else's advert under this company's name."""
    called = []
    monkeypatch.setattr(mp.arbeitsagentur, "ArbeitsagenturSource",
                        lambda c: called.append(1) or _Detail(_detail_payload()))
    posting, _ = await mp.build(
        url=url, text="Text", company="Firma", title="Titel", location="",
        client=client)
    assert called == []            # nothing was fetched
    assert posting.source == mp.MANUAL_SOURCE


@pytest.mark.parametrize("title, location, text", [
    ("Junior Backend", "100% Remote in Deutschland", "Wir suchen dich."),
    ("Junior Backend (remote)", "Berlin", "Wir suchen dich."),
    ("Junior Backend", "Berlin", "Die Stelle ist 100% remote."),
])
async def test_remote_is_read_wherever_it_is_stated(client, title, location, text):
    """Every one of the eight rows entered by hand reads "100% Remote …" in
    the LOCATION and says nothing about it in the title, so reading only
    title+text would drop the flag on exactly the postings he hunts for."""
    posting, _ = await mp.build(url="", text=text, company="Firma",
                                title=title, location=location, client=client)
    assert posting.remote is True


async def test_an_onsite_posting_is_not_called_remote(client):
    posting, _ = await mp.build(url="", text="Vor Ort in Kiel.", company="F",
                                title="Junior Backend", location="Kiel",
                                client=client)
    assert posting.remote is False


async def test_a_pasted_text_is_capped(client):
    posting, _ = await mp.build(url="", text="x" * 40_000, company="F",
                                title="T", location="", client=client)
    assert len(posting.description) == mp.MAX_TEXT


@pytest.mark.parametrize("company, title, refusal", [
    ("", "Titel", mp.NEEDS_COMPANY),
    ("   ", "Titel", mp.NEEDS_COMPANY),
    ("Firma", "", mp.NEEDS_TITLE),
    ("Firma", "  ", mp.NEEDS_TITLE),
])
async def test_company_and_title_are_required(client, company, title, refusal):
    """Both are the KEY the duplicate gate and the cooling-off window compare.
    A posting missing either cannot be checked against anything."""
    posting, got = await mp.build(url="", text="Text", company=company,
                                  title=title, location="", client=client)
    assert posting is None and got == refusal


async def test_an_advert_with_no_text_is_still_accepted(client):
    """Refusing would send the user back to the sqlite prompt, which is the
    behaviour this exists to end. The screen names the cost instead."""
    posting, refusal = await mp.build(url="", text="", company="Firma",
                                      title="Titel", location="", client=client)
    assert refusal == "" and posting.description == ""


# ------------------------------------------------------------------- ids


def test_the_same_advert_twice_derives_the_same_id():
    a = mp.manual_external_id("Beispiel GmbH", "Junior Backend")
    b = mp.manual_external_id("  beispiel  gmbh ", "JUNIOR BACKEND")
    assert a == b        # normalised the way the duplicate check normalises


def test_two_different_adverts_derive_different_ids():
    assert (mp.manual_external_id("Firma A", "Titel")
            != mp.manual_external_id("Firma B", "Titel"))
    assert (mp.manual_external_id("Firma", "Titel A")
            != mp.manual_external_id("Firma", "Titel B"))


def test_a_long_title_still_yields_a_distinct_id():
    """A slug would truncate, and two truncations that collide would file one
    advert as the other."""
    base = "Junior Softwareentwickler fuer verteilte Systeme im Bereich " * 3
    assert (mp.manual_external_id("Firma", base + "Alpha")
            != mp.manual_external_id("Firma", base + "Beta"))


# ----------------------------------------------------------------- gates


async def test_adding_goes_through_the_shared_gate(con, client, monkeypatch):
    """Not "it writes a row" — that it writes it through polling.store_posting,
    the one gate. A second copy of those checks is how a hand-added row walks
    past the cooling-off window."""
    seen = {}
    real = polling.store_posting

    def spy(profile_id, posting):
        seen["profile_id"] = profile_id
        seen["source"] = posting.source
        return real(profile_id, posting)

    monkeypatch.setattr(polling, "store_posting", spy)
    stored, refusal = await mp.add(url="", text="Text", company="Firma X",
                                   title="Junior Dev", location="", client=client)
    assert refusal == "" and stored.outcome == polling.NEW
    assert seen["profile_id"] is None   # it came from no search profile
    assert seen["source"] == mp.MANUAL_SOURCE


async def test_the_stored_row_carries_no_search_profile(con, client):
    stored, _ = await mp.add(url="", text="Text", company="Firma X",
                             title="Junior Dev", location="", client=client)
    row = db.get_job(con, stored.job_id)
    assert row["profile_id"] is None


async def test_the_stored_row_is_unscored(con, client):
    """The scorer judges it, not the person being judged. Unscored is what puts
    it in the batch worker's queue and makes the list say `noch nicht bewertet`."""
    stored, _ = await mp.add(url="", text="Text", company="Firma X",
                             title="Junior Dev", location="", client=client)
    row = db.get_job(con, stored.job_id)
    assert row["match_score"] is None
    assert row["match_reason"] == ""
    assert stored.job_id in [r["id"] for r in db.list_unscored_jobs(con)]


async def test_the_same_advert_added_twice_is_refused(con, client):
    first, _ = await mp.add(url="", text="Text", company="Firma X",
                            title="Junior Dev", location="", client=client)
    second, _ = await mp.add(url="", text="Text", company="Firma X",
                             title="Junior Dev", location="", client=client)
    assert first.outcome == polling.NEW
    assert second.outcome == polling.KNOWN and second.job_id is None


async def test_an_advert_already_known_from_a_board_is_refused(con, client):
    """The cross-source duplicate check compares company+title over every
    source, so pasting an ad discovery already found does not double it."""
    db.insert_job_if_new(con, {
        "profile_id": None, "source": "arbeitsagentur", "external_id": "x-1",
        "title": "Junior Dev", "company": "Firma X",
    })
    con.commit()
    stored, _ = await mp.add(url="", text="Text", company="Firma X",
                             title="Junior Dev", location="", client=client)
    assert stored.outcome == polling.KNOWN


# ------------------------------------------------- what the panel could delete


async def test_build_actually_uses_the_derived_id(con, client):
    """`manual_external_id` was only ever called directly by its own test, so
    deleting the assignment in `build` left every slice test green — and an
    empty external_id defeats UNIQUE(source, external_id) entirely."""
    posting, _ = await mp.build(url="", text="Text", company="Firma X",
                                title="Junior Dev", location="", client=client)
    assert posting.external_id == mp.manual_external_id("Firma X", "Junior Dev")
    assert posting.external_id.startswith("manual-")


async def test_a_fetched_posting_is_stored_with_its_own_link(client, monkeypatch):
    """Nothing pinned the url, so replacing it with '' stayed green — and that
    is the link every "Anzeige öffnen" button on the row opens."""
    monkeypatch.setattr(mp.arbeitsagentur, "ArbeitsagenturSource",
                        lambda c: _Detail(_detail_payload()))
    posting, _ = await mp.build(
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003535918-S",
        text="", company="", title="", location="", client=client)
    assert posting.url.endswith("/10001-1003535918-S")


async def test_a_crashing_fetch_does_not_take_the_page_with_it(
        client, monkeypatch):
    """The `except Exception` arm is commented "a hand-driven action must not
    crash the page" and was never driven. A NiceGUI handler's exception is only
    a log line, so the button would look alive and do nothing."""
    class _Boom:
        name = "arbeitsagentur"

        async def fetch_details(self, posting):
            raise RuntimeError("the API fell over")

    monkeypatch.setattr(mp.arbeitsagentur, "ArbeitsagenturSource",
                        lambda c: _Boom())
    posting, refusal = await mp.build(
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003535918-S",
        text="Von Hand kopiert.", company="Firma", title="Titel",
        location="", client=client)
    assert refusal == ""
    # and it keeps the BA identity, so the text can still heal on a later pass
    assert posting.source == "arbeitsagentur"
    assert posting.description == "Von Hand kopiert."


async def test_the_text_cap_applies_on_the_fetched_branch_too(
        client, monkeypatch):
    """Only the pasted branch's cap was tested, because that test passes
    url=''. Two caps, one of them unguarded."""
    monkeypatch.setattr(
        mp.arbeitsagentur, "ArbeitsagenturSource",
        lambda c: _Detail(_detail_payload(stellenangebotsBeschreibung="")))
    posting, _ = await mp.build(
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003535918-S",
        text="x" * 40_000, company="F", title="T", location="", client=client)
    assert len(posting.description) == mp.MAX_TEXT


async def test_a_pasted_advert_yields_its_application_address(client):
    """Every adapter scans the advert for it, and `insert_job_if_new` settles
    the apply channel from this field AT INSERT TIME — nothing revisits it, so
    a posting that does carry a reachable address stayed `board_apply` for
    ever."""
    posting, _ = await mp.build(
        url="", text="Bewerbungen bitte an bewerbung@firma-x.de senden.",
        company="Firma X", title="Junior Dev", location="", client=client)
    assert posting.contact_email == "bewerbung@firma-x.de"


async def test_a_hand_entered_posting_with_an_address_becomes_an_email_job(
        con, client):
    """The consequence of the line above, through the real writer."""
    stored, _ = await mp.add(
        url="", text="Bewerbung an bewerbung@firma-x.de.",
        company="Firma X", title="Junior Dev", location="", client=client)
    row = db.get_job(con, stored.job_id)
    assert row["contact_email"] == "bewerbung@firma-x.de"
    assert row["apply_channel"] == "direct_email"


async def test_the_cooling_off_window_applies_to_a_hand_entered_posting(
        con, client):
    """Invariant 1's other half, driven end to end rather than asserted about a
    hand-built Stored: a company inside its window must be HELD, and this is
    the exact rule raw SQL walked past four times."""
    db.add_bewerbung(con, {"firma": "Firma X", "status": "Gesendet",
                           "gesendet_am": dates.heute_de(), "kanal": "E-Mail"})
    con.commit()
    stored, _ = await mp.add(url="", text="Text", company="Firma X",
                             title="Ganz andere Stelle", location="",
                             client=client)
    # ADR 0010: it arrives as `new` and is hidden at READ time, so waiting the
    # window out brings it back
    assert stored.outcome == polling.NEW
    row = db.get_job(con, stored.job_id)
    assert row["status"] == "new"
    held = attempts.decide_for_job(con, db.get_job(con, stored.job_id))
    assert held.verdict == identity.COOLING_OFF


async def test_adding_a_posting_never_calls_a_model(con, client, monkeypatch):
    """Invariant 7. The dialog is a button he presses many times an evening and
    the scorer is a paid call; nothing on this path may reach one."""
    import jobdeck.ai.llm as llm

    def explode(*a, **k):
        raise AssertionError("the manual path must never call a model")

    monkeypatch.setattr(llm, "complete", explode)
    monkeypatch.setattr(llm, "web_search", explode, raising=False)
    stored, _ = await mp.add(url="", text="Text", company="Firma X",
                             title="Junior Dev", location="", client=client)
    assert stored.outcome == polling.NEW
    assert db.get_job(con, stored.job_id)["match_score"] is None


def test_two_threads_adding_the_same_advert_admit_exactly_one(con, data_dir):
    """`store_posting` used to have one caller — the poll, which holds a lock
    of its own. It has two now: a press on "Anzeige hinzufügen" can land inside
    a scheduled poll, and without BEGIN IMMEDIATE both read "not here yet"
    before either writes.

    Driven with two real threads rather than argued, like the reservation race
    in test_attempts.py. Same shape: exactly one winner.
    """
    import sqlite3
    import threading

    from jobdeck.sources.base import JobPosting

    start = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def attempt(external_id: str):
        posting = JobPosting(source="manual", external_id=external_id,
                             title="Junior Dev", company="Firma X",
                             description="Text")
        start.wait(timeout=5)
        try:
            outcome = polling.store_posting(None, posting).outcome
        except sqlite3.OperationalError:      # lost the write lock entirely
            outcome = "locked"
        with lock:
            results.append(outcome)

    # DIFFERENT external ids, so UNIQUE(source, external_id) cannot be what
    # decides it — the cross-source duplicate check has to, and that check is
    # read-then-write.
    threads = [threading.Thread(target=attempt, args=("m-1",)),
               threading.Thread(target=attempt, args=("m-2",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert results.count(polling.NEW) == 1, \
        f"expected exactly one winner, got {results}"
    assert con.execute(
        "SELECT COUNT(*) FROM jobs WHERE company='Firma X'").fetchone()[0] == 1


@pytest.mark.parametrize("length, cut", [
    (10, False), (mp.MAX_TEXT, False), (mp.MAX_TEXT + 1, True), (40_000, True),
])
def test_a_cut_advert_can_be_told_from_a_whole_one(length, cut):
    """`posting_text_state` judges the STORED text, so what was cut away is
    invisible to the row marker, the reading pane and the scoring prompt. The
    moment of pasting is the only moment it can be said."""
    assert mp.was_cut("x" * length) is cut
