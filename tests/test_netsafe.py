"""Tests for the shared SSRF guard (IP policy, DNS seam, per-hop URL gate)."""

import ipaddress
import socket

import pytest

from jobdeck import netsafe


@pytest.mark.parametrize("ip, public", [
    ("8.8.8.8", True),
    ("93.184.216.34", True),
    ("10.0.0.1", False),                # RFC 1918
    ("127.0.0.1", False),               # loopback
    ("169.254.7.7", False),             # link-local
    ("100.64.1.1", False),              # CGNAT: is_private AND is_global both False
    ("224.0.0.1", False),               # multicast
    ("0.0.0.0", False),                 # unspecified
    ("2001:4860:4860::8888", True),
    ("::1", False),                     # v6 loopback
    ("fd00::1", False),                 # unique-local
    ("fe80::1", False),                 # v6 link-local
    # A transition address is judged by the IPv4 it transports, NOT by
    # CPython's classification of the v6 range — that varies by patch version
    # (CI caught ::ffff:8.8.8.8 differing from this machine) and is
    # self-inconsistent (it calls 6to4-with-a-public-v4 "private").
    ("::ffff:10.0.0.1", False),         # v4-mapped private (CVE-2024-4032 class)
    ("::ffff:8.8.8.8", True),           # v4-mapped public
    ("64:ff9b::a00:1", False),          # NAT64 embedding 10.0.0.1
    ("64:ff9b::808:808", True),         # NAT64 embedding 8.8.8.8
    ("2002:a00:1::", False),            # 6to4 embedding 10.0.0.1
    ("2002:808:808::", True),           # 6to4 embedding 8.8.8.8
])
def test_ip_is_public(ip, public):
    assert netsafe.ip_is_public(ipaddress.ip_address(ip)) is public


def test_public_literal_host_drops_a_private_ip_but_keeps_hostnames():
    assert netsafe.public_literal_host("http://127.0.0.1:8080/x") == ""
    assert netsafe.public_literal_host("http://[::1]/x") == ""
    assert netsafe.public_literal_host("https://firma.de/jobs") == "firma.de"
    assert netsafe.public_literal_host("firma.de/jobs") == "firma.de"


async def test_a_literal_ip_host_never_consults_the_resolver():
    async def resolver(host):  # a lying resolver must not even be asked
        raise AssertionError("resolver consulted for a literal IP")

    assert await netsafe.host_is_safe("8.8.8.8", resolver=resolver) is True
    assert await netsafe.host_is_safe("10.0.0.1", resolver=resolver) is False
    assert await netsafe.host_is_safe("fe80::1%eth0", resolver=resolver) is False


async def test_all_resolved_addresses_must_be_public():
    async def all_public(host):
        return ["93.184.216.34", "2001:4860:4860::8888"]

    async def mixed(host):  # one private record poisons the whole answer
        return ["93.184.216.34", "10.0.0.5"]

    assert await netsafe.host_is_safe("firma.de", resolver=all_public) is True
    assert await netsafe.host_is_safe("firma.de", resolver=mixed) is False


async def test_resolver_failure_and_empty_answer_fail_closed():
    async def nxdomain(host):
        raise socket.gaierror("NXDOMAIN")

    async def silence(host):
        return []

    assert await netsafe.host_is_safe("firma.de", resolver=nxdomain) is False
    assert await netsafe.host_is_safe("firma.de", resolver=silence) is False
    assert await netsafe.host_is_safe("", resolver=silence) is False


async def test_url_gate_allows_only_absolute_http_urls():
    async def all_public(host):
        return ["93.184.216.34"]

    assert await netsafe.url_is_safe("https://firma.de/x", resolver=all_public)
    assert await netsafe.url_is_safe("http://firma.de", resolver=all_public)
    assert not await netsafe.url_is_safe("file:///etc/passwd", resolver=all_public)
    assert not await netsafe.url_is_safe("ftp://firma.de/x", resolver=all_public)
    assert not await netsafe.url_is_safe("firma.de/x", resolver=all_public)  # relative
    assert not await netsafe.url_is_safe("https://", resolver=all_public)   # no host
    assert not await netsafe.url_is_safe("", resolver=all_public)


async def test_url_gate_checks_the_host_against_dns():
    async def private(host):
        return ["192.168.1.10"]

    assert not await netsafe.url_is_safe("https://intranet.firma.de/x",
                                         resolver=private)


async def test_a_resolver_unicode_error_fails_closed():
    # getaddrinfo raises UnicodeError (not OSError) while IDNA-encoding a
    # malformed label; the guard must treat it as unsafe, not propagate it
    async def idna_reject(host):
        raise UnicodeEncodeError("idna", host, 0, 1, "label empty")

    assert await netsafe.host_is_safe("firma..de", resolver=idna_reject) is False


async def test_an_idn_host_is_resolved_via_the_form_httpx_connects_to():
    # httpx's encode_host sends idna.encode(host.lower()); the guard must
    # check THAT host, not the unicode form getaddrinfo would encode itself
    seen = []

    async def public(host):
        seen.append(host)
        return ["93.184.216.34"]

    assert await netsafe.host_is_safe("straße.de", resolver=public) is True
    assert await netsafe.url_is_safe("https://MÜNCHEN.de/x", resolver=public) is True
    assert seen == ["xn--strae-oqa.de", "xn--mnchen-3ya.de"]


async def test_an_idn_host_resolving_private_is_refused():
    async def private(host):
        return ["192.168.1.10"]

    assert await netsafe.host_is_safe("straße.de", resolver=private) is False


async def test_an_unencodable_idn_host_is_refused_without_resolving():
    # idna rejects the label, exactly as httpx's encode_host would with
    # InvalidURL — fail closed before any resolution
    async def resolver(host):
        raise AssertionError("an unencodable host must not be resolved")

    assert await netsafe.host_is_safe("💩.de", resolver=resolver) is False
    assert await netsafe.url_is_safe("https://💩.de/x", resolver=resolver) is False


@pytest.mark.parametrize("url", [
    "https://www.arbeitnow.com／@evil.example/x",  # netloc changes under NFKC
    "http://[::1",                                # invalid IPv6 literal
    "https://[not-an-ip]/x",
])
def test_a_malformed_url_never_raises_out_of_the_url_helpers(url):
    # urlsplit rejects these with ValueError; every helper must fail closed
    assert netsafe.split_url(url) is None
    assert netsafe.url_hostname(url) == ""
    assert netsafe.public_literal_host(url) == ""
    assert netsafe.is_openable(url) is False


async def test_a_malformed_url_is_refused_by_the_fetch_gate():
    async def resolver(host):
        return ["93.184.216.34"]

    assert await netsafe.url_is_safe("http://[::1", resolver=resolver) is False


@pytest.mark.parametrize("url, openable", [
    ("https://firma.de/stelle", True),
    ("http://firma.de/stelle", True),
    ("javascript:alert(document.domain)", False),
    ("javascript://www.arbeitnow.com/jobs/x/apply", False),
    ("data:text/html,<script>alert(1)</script>", False),
    ("vbscript:msgbox(1)", False),
    ("file:///etc/passwd", False),
    ("//evil.example/x", False),   # protocol-relative: no scheme
    ("/jobs/local", False),
    ("https://", False),           # no host
    ("", False),
])
def test_is_openable_gates_what_the_browser_may_open(url, openable):
    assert netsafe.is_openable(url) is openable
