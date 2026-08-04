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
    ("::ffff:10.0.0.1", False),         # v4-mapped private (CVE-2024-4032 class)
    ("::ffff:8.8.8.8", True),           # v4-mapped public
    ("64:ff9b::a00:1", False),          # NAT64 embedding 10.0.0.1
    # ALL of NAT64 64:ff9b::/96 is is_reserved in CPython — even a public
    # embedded v4 is rejected; conservative, and pinned here on purpose
    ("64:ff9b::808:808", False),
    ("2002:a00:1::", False),            # 6to4 embedding 10.0.0.1
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


async def test_a_non_ascii_host_is_refused_without_resolving():
    # getaddrinfo would IDNA-2003-encode it while httpx sends the IDNA-2008
    # form: the address checked would not be the address connected to
    async def resolver(host):
        raise AssertionError("a non-ASCII host must not be resolved")

    assert await netsafe.host_is_safe("straße.de", resolver=resolver) is False
    assert await netsafe.url_is_safe("https://straße.de/x", resolver=resolver) is False
