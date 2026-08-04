"""Shared network-safety guard for fetching board/posting-derived URLs (SSRF).

Every hop of every outbound fetch must pass two gates BEFORE its request
fires: the scheme is http(s), and the host cannot reach a private network. A
literal-IP host is checked directly; a hostname is resolved and EVERY address
the resolver returns (A and AAAA) must be publicly routable — an attacker
controlling DNS can mix one public record with one private one. An IDN
hostname is resolved via its IDNA ASCII form, the host httpx actually
connects to. The policy
requires ``is_global`` — not merely "not private" — which also rejects CGNAT
100.64.0.0/10, where ``is_private`` and ``is_global`` are BOTH False. IPv6
addresses that embed an IPv4 (v4-mapped, NAT64 64:ff9b::/96, 6to4) have the
embedded address re-checked, independent of the CPython 3.12.4 fix for
CVE-2024-4032. Resolution failure fails CLOSED (unsafe).

The DNS seam (``_system_resolver``) is injectable so tests never touch real
DNS. Accepted residual for a local single-user tool (OWASP's check-then-
connect tier): a rebinding authoritative server could answer public at check
time and private at connect time; closing that needs per-hop IP pinning via a
custom transport — future hardening, not this slice.

``fetch_text`` is the one sanctioned way to GET an untrusted page: the manual
redirect walk clears every hop through the guard before its request fires.
"""

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

import httpx
import idna

log = logging.getLogger(__name__)

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SCHEMES = ("http", "https")

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


async def _system_resolver(host: str) -> list[str]:
    """All addresses (A + AAAA) the system resolver returns for a host."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 an IPv6 address transports, for the transition schemes that can
    smuggle a private v4 behind a globally-routable v6 prefix."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFF_FFFF)
    if ip.sixtofour is not None:
        return ip.sixtofour
    return None


def ip_is_public(ip: _IPAddress) -> bool:
    """Publicly routable per the IANA special-purpose registries.

    An IPv6 address from a transition scheme (v4-mapped, NAT64, 6to4) is judged
    SOLELY by the IPv4 it transports: that is the address packets really reach,
    it is the only thing that can smuggle a private destination behind a
    globally-routable prefix, and — unlike CPython's own classification of
    those ranges, which varies by patch version — it is deterministic."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            return ip_is_public(embedded)
    return ip.is_global and not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    )


def split_url(url: str):
    """`urlsplit` that never raises on untrusted input, returning None instead.

    urlsplit rejects a malformed netloc (an invalid IPv6 literal, a netloc that
    changes under NFKC normalization) with ValueError, and `.hostname` can
    raise for the same reasons — a poisoned URL must fail closed, not kill the
    user's action."""
    try:
        parts = urlsplit(url or "")
        parts.hostname  # noqa: B018 — this is where a bad netloc actually raises
    except ValueError:
        return None
    return parts


def url_hostname(url: str) -> str:
    """Lowercased hostname of a URL, '' when absent or malformed."""
    parts = split_url(url)
    return (parts.hostname or "").lower() if parts is not None else ""


def _literal_ip(host: str) -> _IPAddress | None:
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def public_literal_host(url: str) -> str:
    """Host of a URL, or '' when it is a non-public IP literal. DNS-free — the
    synchronous pre-check for code that only persists/labels a URL; anything
    that FETCHES must pass `url_is_safe` per hop instead."""
    host = url_hostname(url if "://" in url else "//" + url)
    ip = _literal_ip(host) if host else None
    if ip is not None and not ip_is_public(ip):
        return ""
    return host


def is_openable(url: str) -> bool:
    """True when a stored URL is safe to hand to the browser.

    The last gate before `window.open`: a URL reaching the UI may have come
    from a board feed or an employer-supplied field, and a `javascript:` or
    `data:` URL opened from the app's own page would run in its origin.

    An authority carrying a backslash or userinfo is refused even though it
    parses: a browser applies WHATWG rules, which END the authority at a
    backslash, so `https://evil.example\\@join.com/x` is join.com to urlsplit
    and to httpx — the host we screen and label "Bewerbung über JOIN" — but
    evil.example to the click. A URL whose destination depends on who parses
    it must not be offered at all."""
    parts = split_url(url)
    if parts is None or parts.scheme.lower() not in _SCHEMES or not parts.hostname:
        return False
    return "\\" not in parts.netloc and "@" not in parts.netloc


async def host_is_safe(host: str, *, resolver=None) -> bool:
    """True when a host may be fetched: a public IP literal, or a hostname
    whose EVERY resolved address is public. An IDN host is checked via its
    IDNA ASCII form — the exact host httpx connects to. No answer, a resolver
    error or a host that cannot be encoded is unsafe (fail closed)."""
    if not host:
        return False
    ip = _literal_ip(host)
    if ip is not None:
        return ip_is_public(ip)
    if not host.isascii():
        # Resolve the exact ASCII form httpx connects to (its encode_host is
        # idna.encode(host.lower()) — same library, same call). getaddrinfo's
        # own IDNA pass can encode DIFFERENTLY, so resolving the unicode form
        # would check an address other than the one connected to. A host idna
        # rejects fails closed — httpx would refuse it with InvalidURL anyway.
        try:
            host = idna.encode(host.lower()).decode("ascii")
        except idna.IDNAError:
            return False
        # alternate separators (U+3002 and friends) are label dots to idna, so
        # a host can BECOME an IP literal only after encoding — judge it as one
        # rather than trusting the resolver to echo a numeric string back
        ip = _literal_ip(host)
        if ip is not None:
            return ip_is_public(ip)
    if resolver is None:
        resolver = _system_resolver
    try:
        addresses = await resolver(host)
    except (OSError, UnicodeError):
        # OSError: NXDOMAIN / timeout / no resolver (socket.gaierror is one).
        # UnicodeError: getaddrinfo IDNA-encodes the host and rejects an empty
        # or over-long label ("firma..de") — untrusted input must not escape
        return False
    if not addresses:
        return False
    for addr in addresses:
        parsed = _literal_ip(str(addr))
        if parsed is None or not ip_is_public(parsed):
            return False
    return True


async def url_is_safe(url: str, *, resolver=None) -> bool:
    """Gate one fetch hop: an absolute http(s) URL whose host passes
    `host_is_safe`. Call it before EVERY request, including each redirect."""
    parts = split_url(url)
    if parts is None or parts.scheme.lower() not in _SCHEMES:
        return False
    return await host_is_safe((parts.hostname or "").lower(), resolver=resolver)


async def fetch_text(client: httpx.AsyncClient, url: str, *, max_bytes: int,
                     max_redirects: int = 10, deadline: float = 60.0) -> str:
    """GET one untrusted page body, or '' on any failure.

    Bounded three ways, because an httpx timeout is per-operation and bounds
    none of them: `max_redirects` hops, `max_bytes` of body, and `deadline`
    seconds of wall clock for the whole exchange. Without the deadline a server
    trickling one byte every 19 s never trips the client's 20 s read timeout,
    and since the poll job is `max_instances=1` a single such posting silently
    halts ALL job discovery until the app restarts.

    Redirects are walked manually so EVERY hop clears `url_is_safe` before its
    request fires — a redirect chain must not GET a private address any more
    than the first URL may be one. The body streams and STOPS at max_bytes,
    though httpx decompresses one transport chunk at a time, so a compressed
    bomb can still materialize a single inflated chunk before the cap ends the
    loop."""
    try:
        async with asyncio.timeout(deadline):
            return await _fetch_text(client, url, max_bytes, max_redirects)
    except TimeoutError:
        log.info("netsafe: fetch %s exceeded %.0fs — giving up", url, deadline)
        return ""


async def _fetch_text(client: httpx.AsyncClient, url: str,
                      max_bytes: int, max_redirects: int) -> str:
    for _ in range(max_redirects + 1):
        if not await url_is_safe(url):
            return ""
        try:
            async with client.stream("GET", url, follow_redirects=False) as resp:
                if resp.has_redirect_location:
                    url = str(resp.next_request.url)
                    continue
                if resp.status_code != 200:
                    return ""
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= max_bytes:
                        break
                body = b"".join(chunks)[:max_bytes]
                encoding = resp.charset_encoding or "utf-8"
        except Exception as exc:  # network / timeout / malformed URL — non-fatal
            log.info("netsafe: fetch %s failed: %s", url, exc)
            return ""
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:  # a page declaring a charset Python does not know
            return body.decode("utf-8", errors="replace")
    return ""  # too many redirects
