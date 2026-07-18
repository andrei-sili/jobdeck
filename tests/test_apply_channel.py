"""Tests for the deterministic apply-channel classifier."""

import pytest

from jobdeck import apply_channel as ac


def test_a_known_e_mail_wins_over_everything():
    # a company e-mail is the only auto-sendable channel — it beats an ATS host
    r = ac.classify("https://boards.greenhouse.io/acme/jobs/1", contact_email="jobs@acme.de")
    assert r.channel == ac.CHANNEL_DIRECT_EMAIL
    assert r.vendor == ""


@pytest.mark.parametrize("url, vendor", [
    ("https://acme.jobs.personio.de/job/42", "Personio"),
    ("https://karriere.career.softgarden.de/x", "softgarden"),
    ("https://api.softgarden.io/apply/9", "softgarden"),
    ("https://nobix-portal.rexx-systems.com/stellenangebote.html", "rexx systems"),
    ("https://acme.dvinci-hr.com/de/jobs", "d.vinci"),
    ("https://noz.onlyfy.jobs/job/7", "onlyfy"),
    ("https://join.com/companies/mondaai/16443254-staff-engineer", "JOIN"),
    ("https://bewerbermanagement.net/de/jobposting/abc/apply", "BITE"),
    ("https://acme.wd3.myworkdayjobs.com/de/careers/job/1", "Workday"),
    ("https://boards.greenhouse.io/acme/jobs/1", "Greenhouse"),
    ("https://jobs.lever.co/acme/1", "Lever"),
])
def test_known_ats_hosts_are_named(url, vendor):
    r = ac.classify(url)
    assert r.channel == ac.CHANNEL_ATS
    assert r.vendor == vendor


@pytest.mark.parametrize("url, label", [
    ("https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1003292975-S", "Arbeitsagentur"),
    ("https://de.jooble.org/away/12345", "Jooble"),
    ("https://www.arbeitnow.com/jobs/companies/x/y", "Arbeitnow"),
    ("https://www.xing.com/jobs/osnabrueck-ki-154887444", "XING"),
    ("https://jobs.ams.at/public/emps/jobs/abc", "AMS"),
])
def test_known_boards_are_labelled(url, label):
    r = ac.classify(url)
    assert r.channel == ac.CHANNEL_BOARD
    assert r.vendor == label


def test_join_requires_the_companies_or_jobs_path():
    # join.com landing/marketing pages are not an application link
    assert ac.classify("https://join.com/about").channel == ac.CHANNEL_COMPANY_SITE


def test_employer_own_site_is_company_site():
    r = ac.classify("https://mg-systems.de/bewerbung/")
    assert r.channel == ac.CHANNEL_COMPANY_SITE
    assert r.vendor == ""


def test_cname_custom_domain_falls_through_to_company_site():
    # jobs.hoermann.de is a rexx portal behind a custom domain — the host alone
    # cannot reveal the vendor, so it must NOT be mislabelled; it degrades to the
    # generic company-site bucket (the form-action inspection is a later slice)
    assert ac.classify("https://jobs.hoermann.de/x-de-f4796.html").channel \
        == ac.CHANNEL_COMPANY_SITE


def test_suffix_anchor_rejects_lookalike_hosts():
    # a host that merely CONTAINS a vendor token must not match
    assert ac.classify("https://fakepersonio.de/jobs").channel == ac.CHANNEL_COMPANY_SITE
    assert ac.classify("https://acme.jobs.personio.de.evil.com/x").channel \
        == ac.CHANNEL_COMPANY_SITE


def test_missing_scheme_is_tolerated():
    r = ac.classify("acme.jobs.personio.de/job/1")
    assert r.channel == ac.CHANNEL_ATS and r.vendor == "Personio"


def test_empty_or_garbage_url_is_unknown():
    assert ac.classify("").channel == ac.CHANNEL_UNKNOWN
    assert ac.classify("   ").channel == ac.CHANNEL_UNKNOWN
    assert ac.classify(None).channel == ac.CHANNEL_UNKNOWN
