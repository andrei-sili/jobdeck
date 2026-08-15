"""Tests for the on-demand apply-channel resolver (classify + store).

Nothing fetches a Jooble link any more — robots.txt Disallows every shape a
feed result uses — so the SSRF coverage lives on the paths that DO fetch: the
company-site page inspection and the Arbeitnow job page."""

import asyncio

import httpx
import pytest

from jobdeck import apply_channel as ac
from jobdeck.services import apply_resolve


@pytest.fixture(autouse=True)
def _fresh_lock(monkeypatch):
    """A module-level asyncio.Lock binds to the first event loop that awaits
    it, and every async test gets its own — so the lock has to be replaced per
    test or the second one to use it raises."""
    monkeypatch.setattr(apply_resolve, "_lock", asyncio.Lock())


def _job(url, email=""):
    return {"url": url, "contact_email": email}


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_jooble_away_link_is_never_fetched():
    """de.jooble.org/robots.txt Disallows /away/ and /desc/ for User-agent: *
    (and /*?ckey=, which every stored /desc/ link carries). Those are exactly
    the URLs a Jooble result points at, so the polite thing is the only thing:
    never request them, store the link for the HUMAN. Same call as Arbeitnow's
    disallowed apply route.

    Following /away/ was also firing Jooble's click-billing endpoint for a
    visit that never happened."""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            302, headers={"Location": "https://join.com/companies/acme/1-dev"})

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(
            _job("https://de.jooble.org/away/123"), client)
    assert calls == []  # not one request, not even a HEAD
    assert final == "https://de.jooble.org/away/123"
    assert ch.channel == ac.CHANNEL_BOARD and ch.vendor == "Jooble"


async def test_every_disallowed_jooble_shape_is_left_alone():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200)

    for url in ("https://de.jooble.org/away/1",
                "https://de.jooble.org/desc/2",
                "https://de.jooble.org/m/away/3",
                "https://jooble.org/desc/4?ckey=Python+Entwickler",
                "https://de.jooble.org/jdp/5?ckey=x"):
        async with _client(handler) as client:
            _, ch = await apply_resolve.resolve(_job(url), client)
        assert ch.vendor == "Jooble", url
    assert calls == []


async def test_a_jooble_desc_page_is_not_followed():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200)

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(
            _job("https://de.jooble.org/desc/9"), client)
    assert calls == []  # a non-redirector makes no network call
    assert ch.channel == ac.CHANNEL_BOARD and ch.vendor == "Jooble"
    assert final == "https://de.jooble.org/desc/9"


async def test_a_known_email_short_circuits_without_network():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200)

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(
            _job("https://de.jooble.org/away/1", "jobs@acme.de"), client)
    assert calls == []  # a direct e-mail is decisive; no redirect follow
    assert ch.channel == ac.CHANNEL_DIRECT_EMAIL
    assert final == "https://de.jooble.org/away/1"


async def test_a_mixed_public_private_dns_answer_is_rejected(monkeypatch):
    # one private record in the answer poisons the whole host (the OS may
    # connect to any of them) — fail closed
    from jobdeck import netsafe

    async def fake_resolver(host):
        if host == "evil.example.com":
            return ["93.184.216.34", "10.0.0.5"]
        return ["93.184.216.34"]

    monkeypatch.setattr(netsafe, "_system_resolver", fake_resolver)
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, text="<h1>Karriere</h1>")

    # the company-site page inspection is where a fetch still happens
    async with _client(handler) as client:
        _, ch = await apply_resolve.resolve(
            _job("https://evil.example.com/karriere/dev"), client)
    assert calls == []  # never requested: one private record poisons the host
    assert ch.channel == ac.CHANNEL_COMPANY_SITE


async def test_an_unresolvable_host_falls_back_to_the_original_url(monkeypatch):
    import socket

    from jobdeck import netsafe

    async def fake_resolver(host):
        if host == "gone.example.com":
            raise socket.gaierror("NXDOMAIN")
        return ["93.184.216.34"]

    monkeypatch.setattr(netsafe, "_system_resolver", fake_resolver)
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, text="<h1>Karriere</h1>")

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(
            _job("https://gone.example.com/karriere/dev"), client)
    assert calls == []            # resolver failure fails closed, no request
    assert final == "https://gone.example.com/karriere/dev"
    assert ch.channel == ac.CHANNEL_COMPANY_SITE


async def test_a_company_site_page_with_vendor_markers_upgrades_to_ats():
    # jobs.hoermann.de is a rexx portal behind a CNAME — the landing host says
    # company_site, but the page's form action betrays the vendor
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, text=(
            '<form action="https://hoermann.rexx-systems.com/apply/1"></form>'))

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(
            _job("https://jobs.hoermann.de/stelle-42"), client)
    assert final == "https://jobs.hoermann.de/stelle-42"  # URL stays the page
    assert ch.channel == ac.CHANNEL_ATS and ch.vendor == "rexx systems"


async def test_a_company_site_page_without_markers_stays_company_site():
    def handler(request):
        return httpx.Response(200, text="<h1>Karriere</h1><p>Mail uns.</p>")

    async with _client(handler) as client:
        _, ch = await apply_resolve.resolve(
            _job("https://firma.de/karriere/dev"), client)
    assert ch.channel == ac.CHANNEL_COMPANY_SITE and ch.vendor == ""


async def test_a_failing_company_site_fetch_keeps_the_classification():
    def handler(request):
        raise httpx.ConnectError("down")

    async with _client(handler) as client:
        _, ch = await apply_resolve.resolve(
            _job("https://firma.de/karriere/dev"), client)
    assert ch.channel == ac.CHANNEL_COMPANY_SITE


async def test_an_ats_or_board_landing_is_never_page_fetched():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="x")

    async with _client(handler) as client:
        await apply_resolve.resolve(
            _job("https://acme.jobs.personio.de/job/1"), client)
        await apply_resolve.resolve(_job("https://de.jooble.org/desc/2"), client)
    assert calls == []  # the classification was decisive without any network


_AN_JOB = "https://www.arbeitnow.com/jobs/companies/raisin/engineering-lead-81517"
_AN_APPLY = _AN_JOB + "/apply"


async def test_arbeitnow_external_variant_stores_the_apply_deep_link():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=(
            f'<a href="{_AN_APPLY}" target="_blank">'
            '<button class="apply_button_large">Apply Now</button></a>'))

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(_job(_AN_JOB), client)
    assert final == _AN_APPLY
    assert ch.channel == ac.CHANNEL_BOARD and ch.vendor == "Arbeitnow"
    # robots.txt disallows the /apply route to bots: only the JOB page was
    # fetched; the deep-link is stored for the human, never requested
    assert calls == [_AN_JOB]


_AN_UK_JOB = "https://www.arbeitnow.co.uk/jobs/companies/carbonchain/junior-data-engineer-1"
_AN_UK_APPLY = _AN_UK_JOB + "/apply"


async def test_a_uk_posting_resolves_through_the_board_parser_not_a_site_fetch():
    # 13 real postings live on the .co.uk market; before it was registered they
    # read as the employer's own site and earned a company-site page inspection
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=f'<a href="{_AN_UK_APPLY}">Apply Now</a>')

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(_job(_AN_UK_JOB), client)
    assert final == _AN_UK_APPLY
    assert ch.channel == ac.CHANNEL_BOARD and ch.vendor == "Arbeitnow"
    assert calls == [_AN_UK_JOB]  # the job page only — never the /apply route


async def test_an_apply_href_on_the_other_market_is_not_adopted():
    # the two TLDs are separate sites: a .com link on a .co.uk page is exactly
    # the shape a planted anchor takes, and the path check alone would pass it
    crossed = "https://www.arbeitnow.com" + _AN_UK_JOB.split(".co.uk", 1)[1] + "/apply"

    def handler(request):
        return httpx.Response(200, text=f'<a href="{crossed}">Apply</a>')

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(_job(_AN_UK_JOB), client)
    assert final == _AN_UK_JOB  # no deep-link invented, honest board fallback
    assert ch.channel == ac.CHANNEL_BOARD and ch.vendor == "Arbeitnow"


async def test_the_www_variant_of_the_same_market_still_matches():
    # the feed spells the page host with 'www.'; an absolute href without it is
    # the same site and must not be refused
    bare_page = _AN_JOB.replace("www.arbeitnow.com", "arbeitnow.com")

    def handler(request):
        return httpx.Response(200, text=f'<a href="{_AN_APPLY}">Apply</a>')

    async with _client(handler) as client:
        final, _ = await apply_resolve.resolve(_job(bare_page), client)
    assert final == _AN_APPLY


async def test_arbeitnow_join_quick_apply_variant_is_labeled_join():
    def handler(request):
        return httpx.Response(200, text=(
            '<form id="form_job_application" method="POST"></form>'
            f'<div id="text_issues_with_applying">Try the '
            f'<a href="{_AN_APPLY}">company portal</a></div>'))

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(_job(_AN_JOB), client)
    assert final == _AN_APPLY
    assert ch.channel == ac.CHANNEL_ATS and ch.vendor == "JOIN"


async def test_arbeitnow_page_without_the_expected_layout_falls_back():
    def handler(request):
        return httpx.Response(200, text="<h1>Some redesigned page</h1>")

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(_job(_AN_JOB), client)
    assert final == _AN_JOB  # unchanged: no invented deep-link
    assert ch.channel == ac.CHANNEL_BOARD and ch.vendor == "Arbeitnow"


async def test_arbeitnow_fetch_failure_falls_back_to_the_board_label():
    def handler(request):
        raise httpx.ConnectError("down")

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(_job(_AN_JOB), client)
    assert final == _AN_JOB
    assert ch.channel == ac.CHANNEL_BOARD and ch.vendor == "Arbeitnow"


async def test_a_foreign_apply_looking_href_is_not_adopted():
    # only an arbeitnow-hosted /jobs/…/apply link is the board's own deep-link
    def handler(request):
        return httpx.Response(200, text=(
            '<a href="https://evil.example.com/jobs/x/apply">Apply</a>'))

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(_job(_AN_JOB), client)
    assert final == _AN_JOB
    assert ch.channel == ac.CHANNEL_BOARD and ch.vendor == "Arbeitnow"


async def test_an_arbeitnow_posting_with_a_known_email_short_circuits():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="x")

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(
            _job(_AN_JOB, "bewerbung@acme.de"), client)
    assert ch.channel == ac.CHANNEL_DIRECT_EMAIL
    assert final == _AN_JOB and calls == []  # a known e-mail wins, no fetch


async def test_a_non_http_apply_href_is_never_adopted():
    # the page is untrusted; a javascript:/data: href must never become a URL
    # the app hands to the browser
    poisoned = ("javascript://www.arbeitnow.com" + _AN_JOB.split(".com", 1)[1]
                + "/apply")

    def handler(request):
        return httpx.Response(200, text=f'<a href="{poisoned}">Apply</a>')

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(_job(_AN_JOB), client)
    assert final == _AN_JOB
    assert ch.channel == ac.CHANNEL_BOARD and ch.vendor == "Arbeitnow"


async def test_an_apply_href_for_another_job_is_not_adopted():
    # a planted anchor earlier in the document must not hijack the deep-link
    other = "https://www.arbeitnow.com/jobs/companies/evil/other-job-1/apply"

    def handler(request):
        return httpx.Response(200, text=(
            f'<a href="{other}">Related</a><a href="{_AN_APPLY}">Apply Now</a>'))

    async with _client(handler) as client:
        final, _ = await apply_resolve.resolve(_job(_AN_JOB), client)
    assert final == _AN_APPLY  # this posting's own link, not the first match


async def test_a_userinfo_disguised_apply_href_is_not_adopted():
    disguised = ("https://evil.example.com@www.arbeitnow.com"
                 + _AN_JOB.split(".com", 1)[1] + "/apply")

    def handler(request):
        return httpx.Response(200, text=f'<a href="{disguised}">Apply</a>')

    async with _client(handler) as client:
        final, _ = await apply_resolve.resolve(_job(_AN_JOB), client)
    assert final == _AN_JOB


async def test_a_lookalike_board_host_is_not_treated_as_arbeitnow():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="x")

    async with _client(handler) as client:
        _, ch = await apply_resolve.resolve(
            _job("https://evil-arbeitnow.com/jobs/companies/x/1"), client)
    # not the board: it is classified as a company site, and the page fetch
    # that follows is the company-site inspection, not the board parser
    assert ch.channel == ac.CHANNEL_COMPANY_SITE


async def test_a_company_site_redirect_to_a_private_host_is_never_fetched():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "firma.de":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
        return httpx.Response(200, text=(
            '<form action="https://acme.jobs.personio.de/apply"></form>'))

    async with _client(handler) as client:
        _, ch = await apply_resolve.resolve(_job("https://firma.de/karriere/1"), client)
    assert seen == ["firma.de"]  # the metadata endpoint was never requested
    assert ch.channel == ac.CHANNEL_COMPANY_SITE


async def test_an_arbeitnow_page_redirect_to_a_private_host_is_never_fetched():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if "arbeitnow.com" in request.url.host:
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:9/x"})
        return httpx.Response(200, text=f'<a href="{_AN_APPLY}">Apply</a>')

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(_job(_AN_JOB), client)
    assert seen == ["www.arbeitnow.com"]
    assert final == _AN_JOB and ch.channel == ac.CHANNEL_BOARD


async def test_a_poisoned_href_does_not_abort_the_arbeitnow_resolution():
    # a malformed href earlier in the document must be skipped, not raise
    def handler(request):
        return httpx.Response(200, text=(
            '<a href="https://www.arbeitnow.com／@evil.example/x/apply">x</a>'
            f'<a href="{_AN_APPLY}">Apply Now</a>'))

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(_job(_AN_JOB), client)
    assert final == _AN_APPLY and ch.vendor == "Arbeitnow"


async def test_a_malformed_stored_url_resolves_without_raising():
    def handler(request):
        raise AssertionError("a malformed URL must not be fetched")

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(_job("http://[::1"), client)
    assert final == "http://[::1" and ch.channel == ac.CHANNEL_UNKNOWN


async def test_an_apply_href_on_a_foreign_host_with_this_jobs_path_is_rejected():
    # pins the HOST allowlist independently of the path check
    foreign = "https://evil.example" + _AN_JOB.split(".com", 1)[1] + "/apply"

    def handler(request):
        return httpx.Response(200, text=f'<a href="{foreign}">Apply</a>')

    async with _client(handler) as client:
        final, ch = await apply_resolve.resolve(_job(_AN_JOB), client)
    assert final == _AN_JOB and ch.vendor == "Arbeitnow"


async def test_a_join_form_on_a_foreign_host_does_not_use_the_board_parser():
    # the JOIN form-id signal is Arbeitnow-specific: on any other host the page
    # must go through the generic vendor-fingerprint path instead, which finds
    # no vendor here (no join.com resource is loaded)
    def handler(request):
        return httpx.Response(200, text='<form id="form_job_application"></form>')

    async with _client(handler) as client:
        _, ch = await apply_resolve.resolve(
            _job("https://evil-arbeitnow.com/jobs/companies/x/1"), client)
    assert ch.channel == ac.CHANNEL_COMPANY_SITE and ch.vendor == ""


def test_the_robots_disallow_list_matches_what_jooble_publishes():
    """Read from https://de.jooble.org/robots.txt on 2026-08-05, User-agent: *
    group: Disallow /away/, /desc/, /m/away/, /m/desc/ and /*?ckey= — the last
    catching every /desc/ link the app has stored.

    This is a unit test on the predicate because the early return it guards is
    currently defence-in-depth: nothing in resolve() fetches a redirector any
    more, so removing the branch does not change behaviour today. Narrowing
    the RULE, however, is exactly the regression worth catching."""
    disallowed = [
        "https://de.jooble.org/away/123",
        "https://de.jooble.org/desc/9",
        "https://de.jooble.org/m/away/3",
        "https://de.jooble.org/m/desc/4",
        "https://jooble.org/desc/4?ckey=Python+Entwickler",
        "https://de.jooble.org/jdp/5?ckey=x",       # allowed path, disallowed query
        "de.jooble.org/away/7",                      # scheme-less, as stored in the wild
    ]
    for url in disallowed:
        assert ac.is_robots_disallowed(url), url

    allowed = [
        "https://de.jooble.org/",                    # the site root is not disallowed
        "https://join.com/companies/acme/1-dev",     # another host entirely
        "https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1",
        "https://notjooble.org/away/1",              # suffix must not match loosely
        "",
    ]
    for url in allowed:
        assert not ac.is_robots_disallowed(url), url


def _seed(con, rows):
    from jobdeck import db
    ids = []
    for r in rows:
        jid = db.insert_job_if_new(con, {
            "source": "stub", "external_id": r["ext"], "title": "Dev",
            "company": r.get("company", "Firma"), "url": r["url"],
            "description": "d", "contact_email": r.get("email", ""),
        })
        con.execute("UPDATE jobs SET match_score=? WHERE id=?", (r["score"], jid))
        if r.get("channel"):
            db.set_apply_channel(con, jid, r["channel"], "", r["url"])
        ids.append(jid)
    con.commit()
    return ids


async def test_resolve_pending_walks_the_backlog_best_scored_first(con, data_dir):
    """The per-job button is the right shape for one posting and the wrong one
    for a backlog: of 287 scored postings only 8 had a channel, so the rest
    meant that many individual clicks."""
    _seed(con, [
        {"ext": "a", "url": "https://acme.jobs.personio.de/job/1", "score": 90},
        {"ext": "b", "url": "https://de.jooble.org/desc/2", "score": 80},
        {"ext": "c", "url": "https://firma.de/karriere", "score": 70,
         "email": "jobs@firma.de"},
        {"ext": "d", "url": "https://x.de/j", "score": 60, "channel": "ats_form"},
    ])
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text="<h1>Karriere</h1>")

    # a posting that arrived WITH an address never waits for this pass at all
    before = {r["external_id"]: r["apply_channel"]
              for r in con.execute("SELECT external_id, apply_channel FROM jobs")}
    assert before["c"] == ac.CHANNEL_DIRECT_EMAIL

    async with _client(handler) as client:
        res = await apply_resolve.resolve_pending(limit=10, client=client)

    # two left to resolve: the already-resolved one and the e-mail one are both
    # skipped
    assert res["resolved"] == 2
    assert res["failed"] == 0
    assert res["remaining"] == 0
    stored = {r["external_id"]: r["apply_channel"]
              for r in con.execute("SELECT external_id, apply_channel FROM jobs")}
    assert stored["a"] == ac.CHANNEL_ATS
    assert stored["b"] == ac.CHANNEL_BOARD
    assert stored["c"] == ac.CHANNEL_DIRECT_EMAIL   # a known e-mail is decisive
    assert stored["d"] == "ats_form"                # untouched


async def test_resolve_pending_skips_postings_ruled_out_by_a_hard_requirement(
    con, data_dir
):
    """Score 0 means a hard requirement is violated. Resolving where to apply
    to a job he already ruled out is work nobody asked for — and after today's
    scoring fix there are 129 of them."""
    _seed(con, [
        {"ext": "zero", "url": "https://acme.jobs.personio.de/job/9", "score": 0},
        {"ext": "one", "url": "https://acme.jobs.personio.de/job/8", "score": 1},
    ])
    res = await apply_resolve.resolve_pending(limit=10)
    assert res["resolved"] == 1
    row = con.execute(
        "SELECT apply_channel FROM jobs WHERE external_id='zero'").fetchone()
    assert (row["apply_channel"] or "") == ""


async def test_one_failing_posting_does_not_end_the_pass(con, data_dir, monkeypatch):
    """A backlog pass walks other people's sites; something WILL raise. The
    remaining postings must still get resolved, and the failure must be
    counted rather than swallowed."""
    _seed(con, [
        {"ext": "boom", "url": "https://acme.jobs.personio.de/job/1", "score": 90},
        {"ext": "fine", "url": "https://acme.jobs.personio.de/job/2", "score": 80},
    ])
    real = apply_resolve.resolve

    async def exploding(job, client):
        if job["external_id"] == "boom":
            raise RuntimeError("transport exploded")
        return await real(job, client)

    monkeypatch.setattr(apply_resolve, "resolve", exploding)
    res = await apply_resolve.resolve_pending(limit=10)
    assert res["failed"] == 1
    assert res["resolved"] == 1          # the pass carried on
    stored = {r["external_id"]: (r["apply_channel"] or "") for r in con.execute(
        "SELECT external_id, apply_channel FROM jobs")}
    assert stored["fine"] == ac.CHANNEL_ATS
    assert stored["boom"] == ""          # left unresolved, not half-written


def test_the_batch_is_gentler_than_a_human_clicking():
    """It walks other people's sites in a loop, so it must be slower than the
    button it replaces, not faster."""
    assert apply_resolve.BATCH_PAUSE_S >= 0.25
    assert apply_resolve.BATCH_LIMIT <= 100


async def test_a_bounded_pass_takes_the_best_scored_first(con, data_dir):
    """The batch is bounded, so WHICH postings it resolves is a real decision.
    He opens the top of his inbox, so that is what must know its channel after
    one click — not whichever happened to be inserted first."""
    _seed(con, [
        {"ext": "low", "url": "https://acme.jobs.personio.de/job/1", "score": 10},
        {"ext": "top", "url": "https://acme.jobs.personio.de/job/2", "score": 95},
        {"ext": "mid", "url": "https://acme.jobs.personio.de/job/3", "score": 50},
    ])
    res = await apply_resolve.resolve_pending(limit=2)
    assert res["resolved"] == 2
    assert res["remaining"] == 1
    resolved = {r["external_id"] for r in con.execute(
        "SELECT external_id FROM jobs WHERE COALESCE(apply_channel,'')<>''")}
    assert resolved == {"top", "mid"}      # the 10-scorer waits its turn


async def test_remaining_is_the_true_backlog_not_one_page_of_it(con, data_dir):
    """Found on his real data: after resolving 60 of 279 the pass reported "61
    still pending" because it counted with a LIMITed fetch. Telling him 61 when
    219 are left is worse than telling him nothing."""
    _seed(con, [{"ext": f"j{i}", "url": f"https://acme.jobs.personio.de/job/{i}",
                 "score": 50} for i in range(7)])
    res = await apply_resolve.resolve_pending(limit=2)
    assert res["resolved"] == 2
    assert res["remaining"] == 5      # not min(remaining, limit+1) == 3


async def test_a_second_pass_never_walks_the_backlog_alongside_the_first(
        con, data_dir):
    """The pass runs on a schedule now, and Settings can still start one by
    hand at the same moment. `max_instances=1` guards only the scheduled path,
    so without a lock of its own the two would walk the same backlog together
    and double the requests other people's servers see.

    It returns rather than queueing: a click during the scheduled pass should
    answer immediately, not commit him to a second walk of the whole backlog."""
    from jobdeck import db
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "e1", "title": "Python Entwickler",
        "company": "Beispiel GmbH", "url": "https://beispiel.example/1"})
    db.set_job_score(con, job_id, 80, "fits")
    con.commit()
    # the posting really is one this pass would pick up — otherwise the
    # assertions below would hold for a batch that simply had nothing to do
    assert [r["id"] for r in db.jobs_needing_apply_channel(con, 10)] == [job_id]

    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text="<html></html>")

    async with apply_resolve._lock:          # a pass is under way
        async with _client(handler) as client:
            result = await apply_resolve.resolve_pending(client=client)

    assert result["resolved"] == 0
    assert seen == [], "the second pass made requests anyway"
    assert db.get_job(con, job_id)["apply_channel"] == ""
    # and the lock is free again afterwards, so the next tick really runs
    assert not apply_resolve._lock.locked()
