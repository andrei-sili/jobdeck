"""Tests for netsafe.fetch_text — the one guarded way to GET an untrusted page."""

import httpx

from jobdeck import netsafe


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _fetch(handler, url="https://firma.de/impressum", **kw):
    async with _client(handler) as client:
        return await netsafe.fetch_text(client, url, max_bytes=kw.pop("max_bytes", 1000),
                                        **kw)


async def test_a_200_body_is_returned_and_a_non_200_body_is_not():
    # an error page can carry addresses and vendor scripts of its own — only a
    # real 200 may feed the extractors
    def ok(request):
        return httpx.Response(200, text="bewerbung@firma.de")

    def not_found(request):
        return httpx.Response(404, text="webmaster@firma.de")

    def server_error(request):
        return httpx.Response(500, text="ops@firma.de")

    assert await _fetch(ok) == "bewerbung@firma.de"
    assert await _fetch(not_found) == ""
    assert await _fetch(server_error) == ""


async def test_the_body_is_capped_in_bytes_while_streaming():
    def handler(request):
        return httpx.Response(200, content=b"x" * 1_000_000)

    body = await _fetch(handler, max_bytes=500)
    assert len(body) == 500


async def test_a_compressed_bomb_is_not_inflated_past_the_cap():
    import gzip

    payload = gzip.compress(b"0" * 5_000_000)

    def handler(request):
        return httpx.Response(200, content=payload,
                              headers={"content-encoding": "gzip"})

    body = await _fetch(handler, max_bytes=1000)
    assert len(body) <= 1000


async def test_a_relative_location_is_joined_against_the_current_url():
    def handler(request):
        if request.url.path == "/impressum":
            return httpx.Response(302, headers={"Location": "/kontakt"})
        if request.url.path == "/kontakt":
            return httpx.Response(200, text="bewerbung@firma.de")
        return httpx.Response(404)

    assert await _fetch(handler) == "bewerbung@firma.de"


async def test_a_redirect_loop_terminates_and_returns_nothing():
    hops = []

    def handler(request):
        hops.append(str(request.url))
        return httpx.Response(302, headers={"Location": "/loop"})

    async with _client(handler) as client:
        body = await netsafe.fetch_text(client, "https://firma.de/loop",
                                        max_bytes=1000, max_redirects=3)
    assert body == ""
    assert len(hops) == 4  # the cap bounds the walk instead of spinning


async def test_a_redirect_without_a_location_yields_nothing():
    def handler(request):
        return httpx.Response(302)

    assert await _fetch(handler) == ""


async def test_a_malformed_location_on_an_odd_3xx_does_not_raise():
    # httpx pre-validates Location only for the standard redirect codes
    def handler(request):
        return httpx.Response(300, headers={"Location": "http://[::1"})

    assert await _fetch(handler) == ""


async def test_every_hop_is_gated_before_its_request_fires():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "firma.de":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:9/x"})
        return httpx.Response(200, text="bewerbung@firma.de")

    assert await _fetch(handler) == ""
    assert seen == ["firma.de"]  # the private hop was never requested


async def test_a_non_http_scheme_is_refused_without_any_request():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text="x")

    assert await _fetch(handler, url="file:///etc/passwd") == ""
    assert seen == []


async def test_a_malformed_hostname_fails_closed_through_the_real_resolver(
        real_resolver):
    # getaddrinfo IDNA-encodes and raises UnicodeError (not OSError) on an
    # empty or over-long label, before any network I/O — untrusted input must
    # fail closed, never escape as a crash into the UI action
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text="x")

    assert await _fetch(handler, url="https://firma..de/karriere") == ""
    assert await _fetch(handler, url="https://" + "a" * 64 + ".example.com/x") == ""
    assert seen == []


async def test_a_page_encoding_other_than_utf8_is_decoded_by_its_charset():
    def handler(request):
        return httpx.Response(200, content="Grüße: bewerbung@firma.de".encode("latin-1"),
                              headers={"content-type": "text/html; charset=iso-8859-1"})

    assert await _fetch(handler) == "Grüße: bewerbung@firma.de"


async def test_an_unknown_declared_charset_falls_back_instead_of_raising():
    def handler(request):
        return httpx.Response(200, content=b"bewerbung@firma.de",
                              headers={"content-type": "text/html; charset=nonsense-8"})

    assert await _fetch(handler) == "bewerbung@firma.de"


async def test_streaming_stops_pulling_once_the_cap_is_reached():
    # the cap must bound what is DOWNLOADED, not only what is returned:
    # a hostile server must not be able to stream gigabytes into memory
    pulled = []

    async def content():
        for i in range(1000):
            pulled.append(i)
            yield b"y" * 1024

    def handler(request):
        return httpx.Response(200, content=content())

    body = await _fetch(handler, max_bytes=2048)
    assert len(body) == 2048
    assert len(pulled) <= 4  # stopped early instead of draining the response
