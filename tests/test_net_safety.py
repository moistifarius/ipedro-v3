"""The bot must not be usable to fetch things from the operator's network.

A Reddit post's link is whatever a member chose to post, and the bot runs on
a home server. `http://192.168.1.10/snapshot.jpg` as a "meme" had the server
fetch a LAN camera and post it into the chat; redirects widened it.
"""

from __future__ import annotations

import httpx
import pytest

from ipedro import net_safety, reddit
from ipedro.net_safety import PUBLIC_ONLY, UnsafeURL, assert_public


def _resolving(mapping):
    async def resolve(host, port):
        if host not in mapping:
            raise OSError("no such host")
        return mapping[host]
    return resolve


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://i.redd.it/abc.jpg",
    "http://example.com/x.gif",
    "https://93.184.216.34/x.jpg",
    "https://[2606:2800:220:1:248:1893:25c8:1946]/x.jpg",
])
async def test_public_urls_pass(url):
    await assert_public(url)                  # the autouse fixture resolves names publicly


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://192.168.1.10/snapshot.jpg",
    "http://10.0.0.5/x.jpg",
    "http://172.16.0.1/x.jpg",
    "http://127.0.0.1:8080/x.jpg",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/x.jpg",
    "http://[::ffff:192.168.1.10]/x.jpg",
    "http://0.0.0.0/x.jpg",
    "http://localhost/x.jpg",
    "http://printer.local/x.jpg",
    "http://nas.home.arpa/x.jpg",
    "ftp://example.com/x.jpg",
    "file:///etc/passwd",
    "gopher://example.com/",
    "http://user:pass@example.com/x.jpg",
    "http:///x.jpg",
    "not a url",
])
async def test_private_local_and_odd_urls_are_refused(url):
    with pytest.raises(UnsafeURL):
        await assert_public(url)


@pytest.mark.asyncio
async def test_a_name_that_resolves_to_a_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(net_safety, "_resolve", _resolving({"evil.example": ["192.168.1.10"]}))
    with pytest.raises(UnsafeURL, match="non-public"):
        await assert_public("https://evil.example/x.jpg")


@pytest.mark.asyncio
async def test_one_private_address_among_public_ones_is_enough_to_refuse(monkeypatch):
    monkeypatch.setattr(net_safety, "_resolve", _resolving({
        "mixed.example": ["93.184.216.34", "10.0.0.1"]}))
    with pytest.raises(UnsafeURL):
        await assert_public("https://mixed.example/x.jpg")


@pytest.mark.asyncio
async def test_a_name_that_doesnt_resolve_is_refused(monkeypatch):
    monkeypatch.setattr(net_safety, "_resolve", _resolving({}))
    with pytest.raises(UnsafeURL, match="can't resolve"):
        await assert_public("https://nothing.example/x.jpg")


# ── on the wire, through httpx ───────────────────────────────────────────────

def _client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True,
        event_hooks=PUBLIC_ONLY,
    )


@pytest.mark.asyncio
async def test_a_redirect_from_a_public_url_to_a_private_one_is_stopped(monkeypatch):
    """A public URL that 302s to the LAN is the standard way past a check
    made only on the first URL."""
    hit = []

    def handler(request: httpx.Request):
        hit.append(str(request.url))
        if request.url.host == "good.example":
            return httpx.Response(302, headers={"location": "http://192.168.1.10/snap.jpg"})
        return httpx.Response(200, content=b"LAN SECRETS")

    async with _client(handler) as client:
        with pytest.raises(UnsafeURL):
            await client.get("https://good.example/x.jpg")
    assert hit == ["https://good.example/x.jpg"]          # the LAN was never asked


@pytest.mark.asyncio
async def test_a_normal_download_is_untouched():
    def handler(request):
        return httpx.Response(200, content=b"PNG")

    async with _client(handler) as client:
        assert (await client.get("https://i.redd.it/a.png")).content == b"PNG"


@pytest.mark.asyncio
async def test_the_reddit_media_download_refuses_a_lan_url(monkeypatch):
    """The path the finding was about, end to end through reddit's own
    download function."""
    hits = []

    def handler(request):
        hits.append(request)
        return httpx.Response(200, content=b"LAN CAMERA")

    real = httpx.AsyncClient
    monkeypatch.setattr(
        reddit.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    media = reddit.Media(kind="photo", url="http://192.168.1.10/snapshot.jpg")
    assert await reddit.download_media(media, user_agent="t") is None
    assert hits == []
