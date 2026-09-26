"""Offline tests for trmnl_proxy (no network)."""

from __future__ import annotations

import urllib.error

import pytest
import server


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

    def fake_fetch_json(url, *, headers, timeout, retries):
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
    entry = server._CACHE[server._cache_key("advance", "tok-1")]
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
    key = server._cache_key("advance", "tok-1")
    monkeypatch.setitem(server._CACHE[key], "expires_at", 0)

    def too_fast(url, **kw):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(server, "fetch_json", too_fast)
    out = server.fetch({}, {}, ctx={})
    assert out["filename"] == "screen-1"
    assert "error" not in out


def test_error_status_is_reported(settings, monkeypatch):
    monkeypatch.setattr(server, "fetch_json", lambda *a, **k: {"status": 500, "error": "boom"})
    out = server.fetch({}, {}, ctx={})
    assert "status 500" in out["error"]
