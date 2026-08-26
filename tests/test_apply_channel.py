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
    # the board's UK market — same site, second TLD
    ("https://www.arbeitnow.co.uk/jobs/companies/x/y", "Arbeitnow"),
    ("https://www.xing.com/jobs/osnabrueck-ki-154887444", "XING"),
    ("https://jobs.ams.at/public/emps/jobs/abc", "AMS"),
    ("https://www.get-in-it.de/jobsuche/p12345", "get in IT"),
    ("https://germantechjobs.de/jobs/python-developer-berlin", "GermanTechJobs"),
    ("https://persy.jobs/jobs/12345", "Persy"),
    ("https://www.deutschland-stellenmarkt.de/anzeige/12345", "Deutschland-Stellenmarkt"),
    ("https://www.studyflix.de/jobs/detail/1234", "Studyflix"),
])
def test_known_boards_are_labelled(url, label):
    r = ac.classify(url)
    assert r.channel == ac.CHANNEL_BOARD
    assert r.vendor == label


@pytest.mark.parametrize("lookalike", [
    "https://persy.jobs.evil.com/x",
    "https://evilgermantechjobs.de/x",
    "https://get-in-it.de.evil.com/jobsuche/p1",
    "https://studyflix.de.attacker.test/jobs/detail/1",
    "https://arbeitnow.co.uk.evil.com/jobs/companies/x/y",
    "https://evil-arbeitnow.co.uk/jobs/companies/x/y",
])
def test_board_suffix_anchors_reject_lookalike_hosts(lookalike):
    # without the '$' anchor a foreign host inherits a board's trusted label
    assert ac.classify(lookalike).channel == ac.CHANNEL_COMPANY_SITE


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


def test_page_markers_reveal_the_ats_behind_a_custom_domain():
    # the CNAME'd rexx portal from above: the landing host hides the vendor,
    # but the apply form posts to the vendor's domain
    html = """<html><body>
      <form action="https://hoermann.rexx-systems.com/apply/123" method="post">
      </form></body></html>"""
    r = ac.detect_ats_from_page(html)
    assert r is not None
    assert r.channel == ac.CHANNEL_ATS and r.vendor == "rexx systems"


def test_a_vendor_widget_script_is_a_fingerprint():
    html = '<script src="https://cdn.personio.de/widget/v2.js"></script>'
    r = ac.detect_ats_from_page(html)
    assert r is not None and r.vendor == "Personio"


def test_a_vendor_iframe_is_a_fingerprint():
    html = '<iframe src="https://firma.career.softgarden.de/embed"></iframe>'
    r = ac.detect_ats_from_page(html)
    assert r is not None and r.vendor == "softgarden"


def test_an_anchor_link_to_a_vendor_is_not_a_fingerprint():
    # a footer/marketing link is not an application form
    html = '<a href="https://join.com/companies/acme">Powered by JOIN</a>'
    assert ac.detect_ats_from_page(html) is None


def test_relative_and_same_host_markers_carry_no_vendor_signal():
    html = """<form action="/bewerbung/absenden"></form>
              <script src="/assets/app.js"></script>"""
    assert ac.detect_ats_from_page(html) is None


def test_a_lookalike_marker_host_does_not_match():
    html = '<form action="https://join.com.evil.de/x"></form>'
    assert ac.detect_ats_from_page(html) is None


def test_a_plain_company_page_yields_no_detection():
    html = "<html><body><h1>Karriere bei Firma</h1><p>Schreiben Sie uns.</p></body></html>"
    assert ac.detect_ats_from_page(html) is None


def test_hostile_or_malformed_html_never_raises():
    assert ac.detect_ats_from_page("<form action='https://" + "x" * 5000) is None
    assert ac.detect_ats_from_page("&#x27;<><</form action=<script") is None
    assert ac.detect_ats_from_page("") is None
    assert ac.detect_ats_from_page(None) is None


def test_a_vendor_lookalike_suffix_is_not_a_fingerprint():
    # the suffix must be a DOMAIN boundary, not a substring
    assert ac.detect_ats_from_page('<form action="https://notjoin.com/x"></form>') is None
    assert ac.detect_ats_from_page(
        '<script src="https://evil-personio.de/w.js"></script>') is None
    # while a real vendor subdomain still matches
    assert ac.detect_ats_from_page(
        '<script src="https://cdn.join.com/w.js"></script>').vendor == "JOIN"


def test_a_malformed_marker_url_is_skipped_instead_of_raising():
    # a poisoned form action must not kill the detection pass
    html = ('<form action="https://www.arbeitnow.com／@evil.example/x"></form>'
            '<script src="https://cdn.join.com/w.js"></script>')
    r = ac.detect_ats_from_page(html)
    assert r is not None and r.vendor == "JOIN"  # skipped the bad one, found the real


def test_a_malformed_posting_url_classifies_as_unknown():
    assert ac.classify("http://[::1").channel == ac.CHANNEL_UNKNOWN


@pytest.mark.parametrize("url, disallowed", [
    ("https://www.arbeitnow.com/jobs/companies/acme/dev-1", False),
    ("https://www.arbeitnow.com/jobs/companies/acme/dev-1/apply", True),
    ("https://www.arbeitnow.co.uk/jobs/companies/acme/dev-1/apply", True),
    ("https://www.arbeitnow.co.uk/jobs/companies/acme/dev-1/apply/", True),
    ("https://de.jooble.org/away/1", True),
    ("https://de.jooble.org/jdp/5?ckey=x", True),
    ("https://de.jooble.org/", False),
    ("https://firma.de/jobs/companies/x/apply", False),  # not one of the boards
])
def test_one_rule_for_every_route_the_boards_forbid(url, disallowed):
    # three consumers now share it: the resolver, the liveness probe, and every
    # redirect hop either of them walks
    assert ac.is_robots_disallowed(url) is disallowed


def test_the_disallowed_apply_route_is_not_a_fetchable_job_page():
    # every caller of is_arbeitnow_job goes on to FETCH what it approves
    assert ac.is_arbeitnow_job("https://www.arbeitnow.com/jobs/companies/x/y")
    assert not ac.is_arbeitnow_job("https://www.arbeitnow.com/jobs/companies/x/y/apply")


@pytest.mark.parametrize("url", [
    "https://www.arbeitnow.com/jobs/companies/acme/dev-1/apply/.",
    "https://www.arbeitnow.com/jobs/companies/acme/dev-1/apply/x/..",
    "https://www.arbeitnow.com/jobs/companies/acme/dev-1/apply/./deeper/..",
    "https://www.arbeitnow.com/jobs/../jobs/companies/acme/dev-1/apply",
    "https://www.arbeitnow.co.uk/jobs/companies/acme/dev-1/apply/.",
])
def test_a_dot_segment_cannot_smuggle_the_apply_route_past_the_screen(url):
    """An HTTP client removes dot segments before the request goes on the wire
    (RFC 3986), so these paths ARE the forbidden route on the wire while ending
    in something else on paper. Proven against httpx itself below."""
    import httpx
    assert httpx.URL(url).path.endswith("/apply"), "premise: this is what is sent"
    assert ac.is_robots_disallowed(url)
    assert not ac.is_arbeitnow_job(url)


# --------------------------------------------------------------------------
# is_board_domain — a job board may never authorize a write to the ledger
# --------------------------------------------------------------------------
@pytest.mark.parametrize("domain", [
    "arbeitsagentur.de",
    "jooble.org",
    "arbeitnow.com",
    "arbeitnow.co.uk",
    "stepstone.de",
    "indeed.de",
    "indeed.com",
    "xing.com",
    "linkedin.com",
    "persy.jobs",
])
def test_a_board_registrable_domain_is_recognised(domain):
    assert ac.is_board_domain(domain)


def test_the_registrable_name_above_a_board_host_counts_as_the_board():
    """The AMS board is registered at `jobs.ams.at`, but every caller has
    already collapsed a sender address to its registrable domain — so the
    agency writes from `ams.at` and the guard has to see it there."""
    assert ac.is_board_domain("jobs.ams.at")
    assert ac.is_board_domain("ams.at")


@pytest.mark.parametrize("suffix", ["at", "de", "com", "co.uk", "jobs"])
def test_a_bare_public_suffix_is_not_a_board(suffix):
    """Walking up from `jobs.ams.at` stops at the registrable name. Counting
    dots instead would make `.at` — and therefore every Austrian employer's
    domain compared against it — read as the board."""
    assert not ac.is_board_domain(suffix)


@pytest.mark.parametrize("domain", [
    "example.com",
    "kranichbau-example.de",
    "my-stepstone.de",          # a longer name that merely ends the same way
    "stepstone.de.attacker.net",  # the board's name in a label someone else owns
    "arbeitsagentur.de.evil.com",
    "",
    "   ",
])
def test_an_employer_domain_is_not_a_board(domain):
    assert not ac.is_board_domain(domain)


def test_the_domain_question_is_asked_case_and_dot_insensitively():
    """A From header spells the host however the sender felt like."""
    assert ac.is_board_domain("ARBEITSAGENTUR.DE")
    assert ac.is_board_domain("Arbeitsagentur.de.")


def test_every_registered_board_answers_yes_for_its_own_domain():
    """The registry is data and grows; a new board must be protected the day
    it is added, without anyone remembering to extend a second list."""
    from jobdeck.contact_resolve import registrable_domain
    for _label, hosts in ac._BOARDS:
        for host in hosts:
            assert ac.is_board_domain(host), host
            assert ac.is_board_domain(registrable_domain(host)), host


def test_a_subdomain_of_a_board_is_the_board():
    assert ac.is_board_domain("de.jooble.org")
    assert ac.is_board_domain("www.arbeitnow.com")
