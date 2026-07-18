"""Tests for the deterministic company-page e-mail resolver."""

import pytest

from jobdeck import contact_resolve as cr


@pytest.mark.parametrize("host, expected", [
    ("firma.de", "firma.de"),
    ("www.firma.de", "firma.de"),
    ("karriere.firma.de", "firma.de"),
    ("https://firma.de/impressum", "firma.de"),
    ("//firma.de", "firma.de"),
    ("FIRMA.DE", "firma.de"),
    ("localhost", ""),          # single label
    ("", ""),
    ("xn--mnchen-3ya.de", ""),  # punycode homograph — rejected
])
def test_registrable_domain(host, expected):
    assert cr.registrable_domain(host) == expected


def test_prefers_a_dedicated_application_inbox_over_generic():
    page = "Kontakt: info@firma.de · Bewerbungen an bewerbung@firma.de bitte."
    r = cr.resolve_email(page, "firma.de")
    assert r["email"] == "bewerbung@firma.de"
    assert r["dedicated"] is True and r["generic"] is False


def test_a_generic_only_page_is_flagged_and_not_dedicated():
    page = "Impressum — E-Mail: info@firma.de"
    r = cr.resolve_email(page, "www.firma.de")
    assert r["email"] == "info@firma.de"
    assert r["dedicated"] is False and r["generic"] is True


def test_an_off_domain_address_is_rejected():
    # a bewerbung@ pointing at another domain (injected/lookalike) must NOT match
    page = "Bewerbung an bewerbung@evil-lookalike.com"
    assert cr.resolve_email(page, "firma.de") == {
        "email": "", "dedicated": False, "generic": False}


def test_a_named_person_address_matches_but_is_not_dedicated():
    page = "Ansprechpartnerin: Frau Weber, m.weber@firma.de"
    r = cr.resolve_email(page, "firma.de")
    assert r["email"] == "m.weber@firma.de"
    assert r["dedicated"] is False and r["generic"] is False


def test_a_trailing_dot_after_the_address_is_stripped():
    page = "Schicken Sie Ihre Unterlagen an jobs@firma.de."
    assert cr.resolve_email(page, "firma.de")["email"] == "jobs@firma.de"


def test_no_company_domain_returns_empty():
    assert cr.resolve_email("bewerbung@firma.de", "")["email"] == ""


def test_no_email_on_the_page_returns_empty():
    assert cr.resolve_email("Kein Kontakt hier.", "firma.de")["email"] == ""
