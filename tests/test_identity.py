"""The identity corpus: spelling variants, reposts, and distinct roles.

`identity.decide` is the only place that answers "may an application be made
for this posting", so every rule it carries is exercised here against plain
records rather than through a page or a service.
"""

import datetime

import pytest

from jobdeck import identity

TODAY = datetime.date(2026, 8, 24)


def app(**kw) -> identity.Application:
    base = {"id": 1, "company": "Beispiel GmbH", "email": "", "position": "",
            "sent_on": "2026-08-10", "last_contact": ""}
    return identity.Application(**{**base, **kw})


def posting(**kw) -> identity.Posting:
    base = {"company": "Beispiel GmbH", "title": "", "contact_email": "", "job_id": None}
    return identity.Posting(**{**base, **kw})


def decide(post, apps=(), reservations=(), *, window_days=60, today=TODAY):
    return identity.decide(post, list(apps), list(reservations),
                           window_days=window_days, today=today)


# --------------------------------------------------------------------------
# Spelling variants — one company must not become two
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ledger_spelling, posting_spelling",
    [
        ("Beispiel GmbH", "Beispiel® GmbH"),
        ("Beispiel® GmbH", "Beispiel GmbH"),
        ("Müller & Co", "MÜLLER & CO"),
        ("Acme  GmbH", " Acme GmbH "),
        ("Acme­GmbH", "AcmeGmbH"),
    ],
)
def test_a_spelling_variant_is_the_same_company(ledger_spelling, posting_spelling):
    got = decide(posting(company=posting_spelling),
                 [app(company=ledger_spelling)])
    assert got.verdict == identity.COOLING_OFF
    assert got.application_id == 1


def test_two_different_companies_do_not_collide():
    assert decide(posting(company="Acme GmbH"), [app(company="Beta GmbH")]).verdict == (
        identity.ALLOW
    )


# --------------------------------------------------------------------------
# Reposts — the same position is blocked for good
# --------------------------------------------------------------------------
def test_the_same_position_at_the_same_company_is_a_republication():
    got = decide(posting(title="AI & Backend Engineer"),
                 [app(position="AI & Backend Engineer")])
    assert got.verdict == identity.BLOCKED_REPUBLICATION
    assert got.permanent is True
    assert got.reopens_on == ""


def test_a_republication_stays_blocked_long_after_the_window_passed():
    got = decide(posting(title="AI & Backend Engineer"),
                 [app(position="AI & Backend Engineer", sent_on="2020-01-01")])
    assert got.verdict == identity.BLOCKED_REPUBLICATION


def test_a_republication_is_named_even_when_the_window_also_applies():
    """The permanent reason must win, or the screen promises a day it will
    never keep."""
    got = decide(posting(title="AI & Backend Engineer"),
                 [app(id=7, position="Data Engineer", sent_on="2026-08-01"),
                  app(id=8, position="AI & Backend Engineer", sent_on="2026-06-01")])
    assert got.verdict == identity.BLOCKED_REPUBLICATION
    assert got.application_id == 8


def test_two_untitled_rows_are_not_the_same_position():
    """An empty title is a posting whose title failed to store, not a role."""
    got = decide(posting(title=""), [app(position="")])
    assert got.verdict == identity.COOLING_OFF


def test_an_unknown_ledger_position_never_proves_a_republication():
    got = decide(posting(title="Software Engineer"), [app(position="")])
    assert got.verdict == identity.COOLING_OFF


# --------------------------------------------------------------------------
# Distinct roles — held back only while the window runs
# --------------------------------------------------------------------------
def test_another_role_is_held_back_and_says_when_it_returns():
    got = decide(posting(title="Software Engineer"),
                 [app(position="AI & Backend Engineer", sent_on="2026-08-10")])
    assert got.verdict == identity.COOLING_OFF
    assert got.reopens_on == "2026-10-09"
    assert got.position == "AI & Backend Engineer"
    assert got.sent_on == "2026-08-10"


def test_the_window_lifts_on_its_own():
    got = decide(posting(title="Software Engineer"),
                 [app(position="AI & Backend Engineer", sent_on="2026-06-01")])
    assert got.verdict == identity.ALLOW


def test_the_posting_returns_on_the_day_the_window_names():
    """Hidden for exactly `window_days`, offered again on the day after."""
    sent = "2026-06-25"
    day_before = decide(posting(title="X"), [app(position="Y", sent_on=sent)],
                        window_days=60, today=datetime.date(2026, 8, 23))
    on_the_day = decide(posting(title="X"), [app(position="Y", sent_on=sent)],
                        window_days=60, today=datetime.date(2026, 8, 24))
    assert day_before.verdict == identity.COOLING_OFF
    assert day_before.reopens_on == "2026-08-24"
    assert on_the_day.verdict == identity.ALLOW


def test_the_newest_application_at_the_company_sets_the_window():
    got = decide(posting(title="X"),
                 [app(id=3, position="A", sent_on="2026-05-01"),
                  app(id=4, position="B", sent_on="2026-08-20")])
    assert got.application_id == 4
    assert got.reopens_on == "2026-10-19"


def test_a_zero_window_switches_the_rule_off():
    got = decide(posting(title="Software Engineer"),
                 [app(position="AI & Backend Engineer")], window_days=0)
    assert got.verdict == identity.ALLOW


def test_a_zero_window_still_blocks_a_republication():
    got = decide(posting(title="AI & Backend Engineer"),
                 [app(position="AI & Backend Engineer")], window_days=0)
    assert got.verdict == identity.BLOCKED_REPUBLICATION


def test_a_negative_window_reads_as_off():
    got = decide(posting(title="X"), [app(position="Y")], window_days=-5)
    assert got.verdict == identity.ALLOW


# --------------------------------------------------------------------------
# An application nobody can date cannot prove its window passed
# --------------------------------------------------------------------------
@pytest.mark.parametrize("sent_on", ["", "irgendwann", None])
def test_an_undated_application_keeps_the_company_held_back(sent_on):
    got = decide(posting(title="X"), [app(position="Y", sent_on=sent_on)])
    assert got.verdict == identity.COOLING_OFF
    assert got.reopens_on == ""


# --------------------------------------------------------------------------
# A contact address is evidence, never an identity (ADR 0002)
# --------------------------------------------------------------------------
def test_a_shared_address_at_another_company_does_not_block():
    got = decide(posting(company="Acme GmbH", contact_email="jobs@ats.example"),
                 [app(company="Beta GmbH", email="jobs@ats.example")])
    assert got.verdict == identity.ALLOW
    assert got.corroborating_email is True


def test_a_shared_address_is_reported_alongside_a_window_decision():
    got = decide(
        posting(company="Acme GmbH", title="X", contact_email="jobs@ats.example"),
        [app(company="Acme GmbH", position="Y"),
         app(id=2, company="Beta GmbH", email="jobs@ats.example")],
    )
    assert got.verdict == identity.COOLING_OFF
    assert got.corroborating_email is True


def test_the_companys_own_address_is_not_reported_as_corroboration():
    """Evidence means a SECOND company reachable at one address. The company's
    own row is the decision itself, and naming it twice would read as two
    independent findings."""
    got = decide(posting(company="Acme GmbH", contact_email="jobs@acme.example"),
                 [app(company="Acme GmbH", email="jobs@acme.example",
                      sent_on="2026-01-01")])
    assert got.verdict == identity.ALLOW
    assert got.corroborating_email is False


# --------------------------------------------------------------------------
# A live reservation is about this instant, not about policy
# --------------------------------------------------------------------------
def test_a_live_reservation_refuses_a_second_attempt():
    got = decide(posting(title="X"), [],
                 [identity.Reservation(key="job:9", company="Beispiel GmbH")])
    assert got.verdict == identity.RESERVED
    assert got.reservation_key == "job:9"
    assert got.permanent is False


def test_a_reservation_at_another_company_is_irrelevant():
    got = decide(posting(company="Acme GmbH"), [],
                 [identity.Reservation(key="job:9", company="Beispiel GmbH")])
    assert got.verdict == identity.ALLOW


def test_a_republication_outranks_a_live_reservation():
    """Both refuse, but only one of them is still true in a minute."""
    got = decide(posting(title="X"), [app(position="X")],
                 [identity.Reservation(key="job:9", company="Beispiel GmbH")])
    assert got.verdict == identity.BLOCKED_REPUBLICATION


# --------------------------------------------------------------------------
# A posting with no company is missing information, not an employer
# --------------------------------------------------------------------------
def test_a_posting_without_a_company_is_judged_by_nothing():
    got = decide(posting(company="   ", title="X"), [app(company="")])
    assert got.verdict == identity.ALLOW


def test_an_empty_ledger_company_matches_no_posting():
    got = decide(posting(company="Acme GmbH", title="X"), [app(company="")])
    assert got.verdict == identity.ALLOW


# --------------------------------------------------------------------------
# reopens_on is used by callers directly, so it carries its own guarantees
# --------------------------------------------------------------------------
def test_reopens_on_is_empty_without_a_usable_date():
    assert identity.reopens_on("", 60) == ""
    assert identity.reopens_on("not a date", 60) == ""


def test_reopens_on_is_empty_when_the_window_is_off():
    assert identity.reopens_on("2026-08-10", 0) == ""


def test_reopens_on_counts_from_the_day_it_was_sent():
    assert identity.reopens_on("2026-08-10", 60) == "2026-10-09"
    assert identity.reopens_on("2026-08-10T14:30:00", 1) == "2026-08-11"


# --------------------------------------------------------------------------
# The window runs from the last contact, not from the day something was sent
# --------------------------------------------------------------------------
def test_a_later_contact_moves_the_window_past_the_send_date():
    """A ledger row can be months old while the conversation is days old. One
    real row read as sent in June carried a receipt from August, and counting
    from the send date offered a company that had answered thirteen days
    earlier."""
    got = decide(posting(title="X"),
                 [app(position="Y", sent_on="2026-06-12",
                      last_contact="2026-08-11T11:33:12")])
    assert got.verdict == identity.COOLING_OFF
    assert got.sent_on == "2026-06-12", "the ledger date is still the evidence"
    assert got.last_contact == "2026-08-11T11:33:12"
    assert got.reopens_on == "2026-10-10", "sixty days from the CONTACT"


def test_without_a_contact_the_send_date_is_the_anchor():
    got = decide(posting(title="X"), [app(position="Y", sent_on="2026-08-10")])
    assert got.reopens_on == "2026-10-09"
    assert got.last_contact == "2026-08-10"


def test_the_newest_contact_decides_not_the_newest_send():
    """Two applications at one company: the one written to most recently holds
    it, even when the other went out later."""
    got = decide(posting(title="X"),
                 [app(id=3, position="A", sent_on="2026-08-20",
                      last_contact="2026-08-20"),
                  app(id=4, position="B", sent_on="2026-01-01",
                      last_contact="2026-08-23")])
    assert got.application_id == 4
    assert got.reopens_on == "2026-10-22"


def test_a_contact_that_cannot_be_read_falls_back_to_the_send_date():
    got = decide(posting(title="X"),
                 [app(position="Y", sent_on="2026-08-10", last_contact="")])
    assert got.reopens_on == "2026-10-09"
