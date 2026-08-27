"""Recognising a posting URL the user pasted from their browser.

This is the handle that turns "I found an ad" into "JobDeck can fetch it".
The narrowness is the point: a search-results page names no single posting,
and on 2026-08-26 six of the eight rows the user inserted by hand carried
one — three of those urls were shared by two different postings.
"""

import pytest

from jobdeck.sources import arbeitsagentur as ag


@pytest.mark.parametrize("url, refnr", [
    ("https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003535918-S",
     "10001-1003535918-S"),
    # no scheme — what a paste out of the address bar can look like
    ("www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003535918-S",
     "10001-1003535918-S"),
    ("https://arbeitsagentur.de/jobsuche/jobdetail/19301-952981419-S",
     "19301-952981419-S"),
    # a copied link routinely ends in a slash, or carries tracking
    ("https://www.arbeitsagentur.de/jobsuche/jobdetail/13644-265788-S/",
     "13644-265788-S"),
    ("https://www.arbeitsagentur.de/jobsuche/jobdetail/13644-265788-S?x=1",
     "13644-265788-S"),
    ("https://www.arbeitsagentur.de/jobsuche/jobdetail/13644-265788-S#top",
     "13644-265788-S"),
    ("  https://www.arbeitsagentur.de/jobsuche/jobdetail/13644-265788-S  ",
     "13644-265788-S"),
])
def test_a_posting_url_yields_its_referenznummer(url, refnr):
    assert ag.refnr_from_url(url) == refnr


@pytest.mark.parametrize("url", [
    # a SEARCH page names no single posting — this is the exact shape the user
    # pasted eight times, and answering it with a guess would store the wrong ad
    "https://www.arbeitsagentur.de/jobsuche/suche?was=Python",
    "https://www.arbeitsagentur.de/jobsuche/",
    "https://www.arbeitsagentur.de/",
    # a BA path LONGER than the jobdetail prefix, on the real host. Every
    # search url above is shorter than the prefix, so a recognizer that
    # dropped the prefix check would slice them to '' by accident and pass:
    # this is the case that can tell the guard apart from that accident.
    "https://www.arbeitsagentur.de/jobsuche/suche/ergebnisliste/lang",
    "https://www.arbeitsagentur.de/bewerbung/anschreiben-vorlagen-muster",
    # another board entirely
    "https://de.jooble.org/stellenangebote-junior-softwareentwickler-remote",
    "https://www.arbeitnow.com/jobs/companies/acme/dev-1",
    # a lookalike host must not be read as the agency
    "https://arbeitsagentur.de.evil.example/jobsuche/jobdetail/1-2-S",
    "https://notarbeitsagentur.de/jobsuche/jobdetail/1-2-S",
    # deeper than a Referenznummer: not one
    "https://www.arbeitsagentur.de/jobsuche/jobdetail/1-2-S/extra",
    "",
    "   ",
    "not a url at all",
])
def test_anything_that_is_not_one_posting_answers_empty(url):
    assert ag.refnr_from_url(url) == ""


def test_the_recognizer_is_the_inverse_of_the_link_search_really_stores():
    """Pinned against the URL `search()` BUILDS, not against a third copy of
    the literal: hardcoding the shape here left the test green when search
    changed, and a pasted link would then stop resolving."""
    import inspect
    source = inspect.getsource(ag.ArbeitsagenturSource.search)
    assert 'url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"' \
        in source, "search() no longer builds the URL this recogniser inverts"
    refnr = "10001-1003535918-S"
    assert ag.refnr_from_url(
        f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}") == refnr


@pytest.mark.parametrize("url", [
    "javascript:alert(1)//www.arbeitsagentur.de/jobsuche/jobdetail/1-2-S",
    "file://www.arbeitsagentur.de/jobsuche/jobdetail/1-2-S",
    "ftp://www.arbeitsagentur.de/jobsuche/jobdetail/1-2-S",
    "data:text/html,//www.arbeitsagentur.de/jobsuche/jobdetail/1-2-S",
])
def test_only_http_urls_can_name_a_posting(url):
    """The answer becomes a Referenznummer this app then FETCHES, so the scheme
    is screened for the same reason `_screen_external_url` screens it."""
    assert ag.refnr_from_url(url) == ""


def test_a_percent_encoded_referenznummer_is_decoded():
    # detail_url re-encodes it, so a round trip must land on the raw id
    assert ag.refnr_from_url(
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001%2D1003535918%2DS"
    ) == "10001-1003535918-S"
