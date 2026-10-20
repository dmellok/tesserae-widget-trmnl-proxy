"""Offline tests for trmnl_proxy (no network)."""

from __future__ import annotations

import urllib.error

import pytest
import server
from flask import Flask

from app.net_guard import BlockedURLError


@pytest.fixture(autouse=True)
def _clean_cache():
    server._CACHE.clear()
    yield
    server._CACHE.clear()


@pytest.fixture
def settings(monkeypatch):
    block: dict = {"access_token_secret": "tok-1"}
    monkeypatch.setattr(server, "_settings", lambda: block)
    return block


@pytest.fixture
def upstream(monkeypatch):
    """Record every upstream call; answer with a fresh filename each
    time so a repeat call is visible in the result."""
    calls: list[tuple[str, dict]] = []

    def fake_fetch_json(url, *, headers, timeout, max_bytes, allow_local):
        assert allow_local is True
        calls.append((url, headers))
        return {
            "status": 0,
            "image_url": f"https://cdn.example/{len(calls)}.png",
            "filename": f"screen-{len(calls)}",
            "refresh_rate": 900,
        }

    monkeypatch.setattr(server, "fetch_json", fake_fetch_json)
    return calls


def test_missing_token_is_a_friendly_error(monkeypatch):
    monkeypatch.setattr(server, "_settings", lambda: {})
    out = server.fetch({}, {}, ctx={})
    assert "device API key" in out["error"]


def test_default_mode_advances_the_playlist(settings, upstream):
    # #331: with the TRMNL device pointed at Tesserae nothing else
    # polls /api/display, so mirroring current_screen froze forever.
    out = server.fetch({}, {}, ctx={"cell_w": 800, "cell_h": 480})
    assert out["mode"] == "advance"
    assert upstream[0][0] == "https://usetrmnl.com/api/display"
    assert upstream[0][1]["Access-Token"] == "tok-1"
    assert upstream[0][1]["Width"] == "800"
    assert out["image_url"].startswith("/plugins/trmnl_proxy/image?")
    assert "f=screen-1" in out["image_url"]


def test_mirror_mode_uses_current_screen(settings, upstream):
    settings["mode"] = "mirror"
    out = server.fetch({}, {}, ctx={})
    assert out["mode"] == "mirror"
    assert upstream[0][0] == "https://usetrmnl.com/api/current_screen"


def test_unknown_mode_falls_back_to_default(settings, upstream):
    settings["mode"] = "whatever"
    assert server.fetch({}, {}, ctx={})["mode"] == "advance"


def test_cache_window_stops_repeat_calls_from_stepping_playlist(settings, upstream):
    a = server.fetch({}, {}, ctx={})
    b = server.fetch({}, {}, ctx={})
    assert a["filename"] == b["filename"] == "screen-1"
    assert len(upstream) == 1


def test_expired_window_fetches_the_next_screen(settings, upstream, monkeypatch):
    server.fetch({}, {}, ctx={})
    entry = server._CACHE[
        server._cache_key("advance", "tok-1", server.DEFAULT_BASE_URL)
    ]
    monkeypatch.setitem(entry, "expires_at", 0)
    out = server.fetch({}, {}, ctx={})
    assert out["filename"] == "screen-2"
    assert len(upstream) == 2


def test_switching_mode_starts_a_fresh_window(settings, upstream):
    server.fetch({}, {}, ctx={})
    settings["mode"] = "mirror"
    server.fetch({}, {}, ctx={})
    assert [u for u, _ in upstream] == [
        "https://usetrmnl.com/api/display",
        "https://usetrmnl.com/api/current_screen",
    ]


def test_rate_limited_serves_stale_envelope(settings, upstream, monkeypatch):
    server.fetch({}, {}, ctx={})
    key = server._cache_key("advance", "tok-1", server.DEFAULT_BASE_URL)
    monkeypatch.setitem(server._CACHE[key], "expires_at", 0)

    def too_fast(url, **kw):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(server, "fetch_json", too_fast)
    out = server.fetch({}, {}, ctx={})
    assert out["filename"] == "screen-1"
    assert "error" not in out


def test_error_status_is_reported(settings, monkeypatch):
    monkeypatch.setattr(
        server, "fetch_json", lambda *a, **k: {"status": 500, "error": "boom"}
    )
    out = server.fetch({}, {}, ctx={})
    assert "status 500" in out["error"]


# ----- base_url (self-hosted LaraPaper / BYOS) ------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "https://usetrmnl.com"),
        ("", "https://usetrmnl.com"),
        ("   ", "https://usetrmnl.com"),
        ("https://usetrmnl.com/", "https://usetrmnl.com"),
        ("  http://192.168.1.20:4567/  ", "http://192.168.1.20:4567"),
        ("http://larapaper.local//", "http://larapaper.local"),
        ("https://example.com/trmnl/", "https://example.com/trmnl"),
        ("http://[::1]:4567", "http://[::1]:4567"),
    ],
)
def test_normalise_base_url_accepts(raw, expected):
    assert server.normalise_base_url(raw) == (expected, None)


@pytest.mark.parametrize(
    "raw",
    [
        "larapaper.local:4567",
        "ftp://larapaper.local",
        "file:///etc/passwd",
        "http://",
        "http://host:notaport",
        "http://host/?x=1",
        "http://user:pw@host",
        "http://[::1",
        42,
    ],
)
def test_normalise_base_url_rejects_with_default(raw):
    base, err = server.normalise_base_url(raw)
    assert base == "https://usetrmnl.com"
    assert err


def test_cache_key_includes_base_url():
    a = server._cache_key("advance", "tok", "https://usetrmnl.com")
    b = server._cache_key("advance", "tok", "http://larapaper.local")
    assert a != b


def test_custom_base_url_is_used_for_the_api_call(settings, upstream):
    settings["base_url"] = " http://192.168.1.20:4567/ "
    out = server.fetch({}, {}, ctx={"cell_w": 800, "cell_h": 480})
    assert "error" not in out
    url, headers = upstream[0]
    assert url == "http://192.168.1.20:4567/api/display"
    assert headers["Access-Token"] == "tok-1"
    assert headers["User-Agent"] == server.USER_AGENT


def test_switching_server_starts_a_fresh_window(settings, upstream):
    server.fetch({}, {}, ctx={})
    settings["base_url"] = "http://larapaper.local"
    out = server.fetch({}, {}, ctx={})
    assert out["filename"] == "screen-2"
    assert [u for u, _ in upstream] == [
        "https://usetrmnl.com/api/display",
        "http://larapaper.local/api/display",
    ]


def test_invalid_base_url_is_an_error_and_makes_no_call(settings, upstream):
    settings["base_url"] = "larapaper.local:4567"
    out = server.fetch({}, {}, ctx={})
    assert "Server URL" in out["error"]
    assert upstream == []


def test_blocked_base_url_is_reported(settings, monkeypatch):
    settings["base_url"] = "http://169.254.169.254"

    def blocked(url, **kw):
        raise BlockedURLError("host is link-local/metadata")

    monkeypatch.setattr(server, "fetch_json", blocked)
    out = server.fetch({}, {}, ctx={})
    assert "not allowed" in out["error"]


def test_metadata_host_blocked_by_real_guard(settings):
    # No mock: the real net_guard refuses link-local before any socket
    # connect, even with allow_local=True.
    settings["base_url"] = "http://169.254.169.254"
    out = server.fetch({}, {}, ctx={})
    assert "not allowed" in out["error"]


@pytest.fixture
def client(settings):
    app = Flask(__name__)
    app.register_blueprint(server.blueprint(), url_prefix="/plugins/trmnl_proxy")
    return app.test_client()


def test_image_route_fetches_through_guard_and_resolves_relative_paths(
    settings, client, monkeypatch
):
    settings["base_url"] = "http://127.0.0.1:4567"
    monkeypatch.setattr(
        server,
        "fetch_json",
        lambda url, **kw: {
            "status": 0,
            "image_url": "/storage/images/a.png",
            "filename": "a",
        },
    )
    fetched: list[tuple[str, dict]] = []

    def fake_fetch_bytes(url, *, headers, timeout, max_bytes, allow_local):
        fetched.append((url, {"headers": headers, "allow_local": allow_local}))
        return b"PNGDATA", ""

    monkeypatch.setattr(server, "fetch_bytes", fake_fetch_bytes)
    resp = client.get("/plugins/trmnl_proxy/image?w=800&h=480")
    assert resp.status_code == 200
    assert resp.data == b"PNGDATA"
    assert resp.mimetype == "image/png"
    url, meta = fetched[0]
    assert url == "http://127.0.0.1:4567/storage/images/a.png"
    assert meta["allow_local"] is True
    assert meta["headers"] == {"User-Agent": server.USER_AGENT}

    # Second hit within the window is served from cache.
    client.get("/plugins/trmnl_proxy/image?w=800&h=480")
    assert len(fetched) == 1


def test_image_route_blocked_image_host_is_502(settings, client, monkeypatch):
    monkeypatch.setattr(
        server,
        "fetch_json",
        lambda url, **kw: {"status": 0, "image_url": "http://169.254.169.254/x.png"},
    )

    def blocked(url, **kw):
        raise BlockedURLError("nope")

    monkeypatch.setattr(server, "fetch_bytes", blocked)
    assert client.get("/plugins/trmnl_proxy/image").status_code == 502


def test_image_route_invalid_base_url_is_502(settings, client):
    settings["base_url"] = "not a url"
    assert client.get("/plugins/trmnl_proxy/image").status_code == 502
