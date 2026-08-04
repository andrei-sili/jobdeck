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
    ("firma.de.", "firma.de"),               # trailing dot normalized
    ("jobs.example.co.uk", "example.co.uk"),  # multi-label public suffix
    ("a.b.example.github.io", "example.github.io"),  # private-section tenant
    ("github.io", ""),          # a bare shared suffix is not registrable
    ("firma.notarealtld", ""),  # off-list TLD -> unverifiable -> no match
    ("192.168.1.1", ""),        # an IP literal is never a registrable domain
    ("localhost", ""),          # single label
    ("", ""),
    ("xn--mnchen-3ya.de", ""),  # punycode homograph — rejected
])
def test_registrable_domain(host, expected):
    assert cr.registrable_domain(host) == expected


def test_a_shared_suffix_tenant_does_not_match_its_neighbour():
    # two tenants of the same hosting suffix are DIFFERENT organizations: an
    # address on evil.github.io must not verify against firma.github.io
    page = "Bewerbung an bewerbung@evil.github.io"
    assert cr.resolve_email(page, "firma.github.io")["email"] == ""


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


def test_an_unverifiable_anchor_verifies_nothing():
    # both sides yield no registrable domain — they must NOT match each other
    assert cr.registrable_domain("github.io") == ""
    assert cr.resolve_email("bewerbung@github.io", "github.io")["email"] == ""
    assert cr.registrable_domain("firma.notarealtld") == ""
    assert cr.resolve_email(
        "bewerbung@evil.notarealtld", "firma.notarealtld")["email"] == ""


@pytest.mark.parametrize("malformed", [
    "[::1",                 # invalid IPv6 literal — urlsplit raises here
    "firma.de／x",          # netloc changes under NFKC normalization
    "https://[not-an-ip]/x",
])
def test_a_malformed_host_yields_no_domain_instead_of_raising(malformed):
    # the anchor and the addresses both come from an UNTRUSTED page; a value
    # urlsplit refuses must fail closed, not abort the user's action
    assert cr.registrable_domain(malformed) == ""
    assert cr.resolve_email("bewerbung@firma.de", malformed)["email"] == ""
