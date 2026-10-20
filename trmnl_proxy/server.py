"""trmnl_proxy, render the next-up TRMNL screen on a Tesserae cell.

Single-account plugin: one ``access_token`` in plugin settings drives
every cell. ``fetch()`` produces a same-origin proxy URL that carries
the cell's pixel dims as query params. The blueprint's ``/image``
route picks those up, asks TRMNL for the envelope with the matching
``Width`` / ``Height`` / ``png-width`` / ``png-height`` headers, and
serves the resulting image bytes back to the cell.

Two upstream endpoints, picked by the ``mode`` setting:

* ``advance`` (default) calls ``GET /api/display``, the endpoint the
  TRMNL firmware itself uses. Every call advances the account's
  playlist by one slot, which is what you want when the TRMNL device
  now points at Tesserae (BYOS) and nothing else is driving the
  playlist. Without this the screen never changes (#331).
* ``mirror`` calls ``GET /api/current_screen``, which returns whatever
  the physical device last painted and never advances the playlist.
  Right when a real TRMNL device still polls TRMNL's servers and the
  Tesserae cell should show the same thing. Per TRMNL's docs this
  endpoint is "designed for our Chrome extension, please don't abuse
  it".

Either way we rate-limit ourselves to at most one upstream call per
``RATE_LIMIT_S`` (currently 60s) per token, or per the envelope's own
``refresh_rate`` when that's longer, by caching the envelope + the
image bytes in process memory. In ``advance`` mode that cache is what
keeps several cells on several pages from stepping the playlist more
than once per window.

The proxy hop exists for two reasons:

* **Mixed-content safety on mobile Safari.** Tesserae is served over
  HTTP on the LAN; TRMNL's image CDN is HTTPS. Desktop browsers
  allow HTTPS subresources on HTTP pages; iOS Safari blocks them
  in some configurations. Same-origin proxy sidesteps it.
* **Token hygiene.** The cell never holds the access token in any
  URL it builds. Only the host-side TRMNL call carries it.

The ``base_url`` setting points the widget at a self-hosted server
that speaks the same API (LaraPaper, or another BYOS server) instead of
usetrmnl.com. Because that URL is operator-chosen, both outbound
fetches (the JSON envelope and the image download) go through
``app.net_guard`` with ``allow_local=True``: loopback and LAN hosts are
allowed so a server on the same box or network works, while link-local
/ cloud-metadata addresses stay blocked and every redirect hop is
re-checked.

No multi-account admin pages. Tesserae's standard settings form
renders the ``access_token``, ``mode`` and ``base_url`` fields
automatically thanks to the manifest declaration; nothing custom
needed.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.parse
from typing import Any

from flask import Blueprint, Response, abort, current_app, request

from app.net_guard import BlockedURLError, fetch_bytes, fetch_json

logger = logging.getLogger(__name__)

TRMNL_BASE = "https://usetrmnl.com"
DEFAULT_BASE_URL = TRMNL_BASE
USER_AGENT = "tesserae-trmnl-proxy/0.3"
HTTP_TIMEOUT_S = 12
PROXY_TIMEOUT_S = 20
# Upper bound on the envelope and image bodies. TRMNL / LaraPaper
# screens are small 1-bit or greyscale PNG/BMP files; this only stops
# a misbehaving server streaming something huge into memory.
ENVELOPE_MAX_BYTES = 1 * 1024 * 1024
IMAGE_MAX_BYTES = 16 * 1024 * 1024
# Floor on time between upstream calls. TRMNL's docs explicitly ask
# callers not to abuse current_screen, and in advance mode every call
# steps the playlist; this rate-limits us per token regardless of how
# many cells refresh at what cadence. The envelope's own
# ``refresh_rate`` value extends this when it's larger.
RATE_LIMIT_S = 60

MODE_ADVANCE = "advance"
MODE_MIRROR = "mirror"
DEFAULT_MODE = MODE_ADVANCE
_ENDPOINTS = {
    MODE_ADVANCE: "/api/display",
    MODE_MIRROR: "/api/current_screen",
}


# ----- plugin settings access -----------------------------------------


def _settings() -> dict[str, Any]:
    """Pull the plugin's persisted settings out of the host store. The
    ``_secret``-suffixed key is the same convention Tesserae's settings
    UI writes when ``secret: true`` is set in the manifest."""
    store = current_app.config["SETTINGS_STORE"]
    section = store.get_section("plugins") or {}
    block = section.get("trmnl_proxy") or {}
    if not isinstance(block, dict):
        return {}
    return block


def _access_token() -> str:
    s = _settings()
    raw = s.get("access_token_secret") or s.get("access_token") or ""
    return raw.strip() if isinstance(raw, str) else ""


def _mode() -> str:
    """``advance`` unless the operator explicitly picked ``mirror``.
    Unknown or missing values fall back to the default so an install
    that predates the setting behaves like a fresh one."""
    raw = _settings().get("mode")
    if isinstance(raw, str) and raw.strip().lower() in _ENDPOINTS:
        return raw.strip().lower()
    return DEFAULT_MODE


def normalise_base_url(raw: Any) -> tuple[str, str | None]:
    """Return ``(base_url, error)`` for the ``base_url`` setting.

    Blank or missing means the TRMNL default. Whitespace and trailing
    slashes are stripped. Anything that isn't an http(s) URL with a
    host (or that carries a query / fragment) is rejected: the
    returned base falls back to the default and ``error`` explains
    what was wrong, so callers can surface it instead of crashing.
    """
    if raw is None:
        return DEFAULT_BASE_URL, None
    if not isinstance(raw, str):
        return DEFAULT_BASE_URL, "Server URL must be text."
    value = raw.strip().rstrip("/")
    if not value:
        return DEFAULT_BASE_URL, None
    invalid = (
        f"Server URL {value!r} isn't valid. Use something like "
        "http://larapaper.local:4567 or leave it blank for usetrmnl.com."
    )
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        parsed.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return DEFAULT_BASE_URL, invalid
    if (
        parsed.scheme.lower() not in ("http", "https")
        or not host
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        return DEFAULT_BASE_URL, invalid
    return value, None


def _base_url() -> tuple[str, str | None]:
    return normalise_base_url(_settings().get("base_url"))


def _dim_headers(width: int, height: int) -> dict[str, str]:
    """Both header pairs TRMNL firmwares historically read, so
    whichever convention this token's device kind respects we
    cover. If TRMNL ignores both we still get back the native
    device-kind resolution; the cell's CSS ``object-fit`` picks
    up the slack."""
    if width <= 0 or height <= 0:
        return {}
    return {
        "Width": str(width),
        "Height": str(height),
        "png-width": str(width),
        "png-height": str(height),
    }


# ----- shared cache ---------------------------------------------------


_CACHE_LOCK = threading.Lock()
# cache key -> {expires_at, envelope, image_bytes, content_type}.
# ``None`` values mean "envelope cached but image not yet fetched".
# One entry per (server, mode, token); multiple cells with the same
# token share state, and flipping the mode or server setting starts a
# fresh window instead of serving the other endpoint's leftovers.
_CACHE: dict[str, dict[str, Any]] = {}


def _cache_key(mode: str, token: str, base_url: str = DEFAULT_BASE_URL) -> str:
    return f"{base_url}|{mode}:{token}"


def _cache_get(key: str) -> dict[str, Any] | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        if entry["expires_at"] <= time.time():
            return None
        return entry


def _cache_store_envelope(key: str, envelope: dict[str, Any]) -> dict[str, Any]:
    """TTL is ``max(RATE_LIMIT_S, envelope.refresh_rate)`` so when
    TRMNL says "next refresh in 900s" we honour it; otherwise we
    floor at 60s. Returns the cache entry."""
    refresh_rate = envelope.get("refresh_rate")
    if isinstance(refresh_rate, int | float) and refresh_rate > 0:
        ttl = max(RATE_LIMIT_S, int(refresh_rate))
    else:
        ttl = RATE_LIMIT_S
    entry = {
        "expires_at": time.time() + ttl,
        "envelope": envelope,
        "image_bytes": None,
        "content_type": None,
    }
    with _CACHE_LOCK:
        _CACHE[key] = entry
    return entry


def _cache_store_image(key: str, body: bytes, content_type: str) -> None:
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is not None:
            entry["image_bytes"] = body
            entry["content_type"] = content_type


def _stale_entry(key: str) -> dict[str, Any] | None:
    """Return the cached entry even if expired. Used after a 429 / 409
    rate-limit response so the panel keeps painting the last good
    frame instead of erroring."""
    with _CACHE_LOCK:
        return _CACHE.get(key)


def _fetch_envelope(
    mode: str,
    token: str,
    cell_w: int,
    cell_h: int,
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, Any]:
    """Call the endpoint for ``mode`` on ``base_url`` and return the
    parsed envelope. Respects the in-process cache; populates it on
    success. On HTTP 429 / 409 (rate limit), returns the stale
    envelope when one exists, else raises."""
    key = _cache_key(mode, token, base_url)
    cached = _cache_get(key)
    if cached is not None:
        return cached["envelope"]
    headers = {
        "Access-Token": token,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": USER_AGENT,
        **_dim_headers(cell_w, cell_h),
    }
    try:
        envelope = fetch_json(
            f"{base_url}{_ENDPOINTS[mode]}",
            headers=headers,
            timeout=HTTP_TIMEOUT_S,
            max_bytes=ENVELOPE_MAX_BYTES,
            allow_local=True,
        )
    except urllib.error.HTTPError as exc:
        # TRMNL surfaces "too fast" via 429 (standard) but 409 has
        # also been observed. Either way, fall back to whatever we
        # last cached for this token, even if formally expired.
        if exc.code in (409, 429):
            stale = _stale_entry(key)
            if stale is not None and stale.get("envelope"):
                logger.info(
                    "trmnl_proxy: rate-limited by upstream (HTTP %s); serving stale envelope",
                    exc.code,
                )
                return stale["envelope"]
        raise
    if not isinstance(envelope, dict):
        raise ValueError("TRMNL returned a non-dict envelope")
    _cache_store_envelope(key, envelope)
    return envelope


# ----- plugin contract ------------------------------------------------


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    """Resolve the next-up TRMNL screen and return a same-origin proxy
    URL the cell client paints. The actual TRMNL round-trip happens
    once per cache window per token, shared across cells."""
    del settings
    token = _access_token()
    if not token:
        return {
            "error": "Paste a TRMNL device API key under Settings → Widgets → TRMNL proxy.",
        }
    mode = _mode()
    base_url, base_err = _base_url()
    if base_err:
        # Don't fall through to usetrmnl.com: the key was most likely
        # issued by the self-hosted server the operator meant to type,
        # and sending it to a different host would leak it.
        return {"error": base_err}

    cell_w = int(ctx.get("cell_w") or 0)
    cell_h = int(ctx.get("cell_h") or 0)

    try:
        envelope = _fetch_envelope(mode, token, cell_w, cell_h, base_url)
    except BlockedURLError as exc:
        return {"error": f"Server URL {base_url} is not allowed: {exc}"}
    except Exception as exc:
        return {"error": f"TRMNL API failure: {type(exc).__name__}: {exc}"}

    status = envelope.get("status")
    # ``/api/display`` answers 0 on success and 202 when the device
    # still needs setup on the TRMNL side (it ships a setup image with
    # that, which is the right thing to paint). ``current_screen``
    # answers 200. Only flag explicit error statuses.
    if isinstance(status, int) and status >= 400:
        return {
            "error": (
                f"TRMNL said status {status}. Check the device on the TRMNL dashboard."
            ),
        }

    filename = envelope.get("filename") or ""
    q = urllib.parse.urlencode(
        {"w": cell_w or "", "h": cell_h or "", "f": filename},
    )
    return {
        "image_url": f"/plugins/trmnl_proxy/image?{q}",
        "filename": filename,
        "refresh_rate_s": envelope.get("refresh_rate"),
        "mode": mode,
        "scale": options.get("scale") or "fill",
    }


# ----- admin blueprint (image proxy only) -----------------------------


def blueprint() -> Blueprint:
    bp = Blueprint("trmnl_proxy_admin", __name__)

    @bp.get("/image")
    def serve_image() -> Response:
        """Cell client hits this. Reuses the per-token envelope cache;
        if the image bytes are already cached for this window, serves
        them directly without touching TRMNL's CDN again. Same-origin
        to avoid mobile-Safari mixed-content blocks."""
        token = _access_token()
        if not token:
            abort(404)
        mode = _mode()
        base_url, base_err = _base_url()
        if base_err:
            abort(502)
        key = _cache_key(mode, token, base_url)

        try:
            cell_w = int(request.args.get("w") or 0)
            cell_h = int(request.args.get("h") or 0)
        except ValueError:
            cell_w = cell_h = 0

        try:
            envelope = _fetch_envelope(mode, token, cell_w, cell_h, base_url)
        except Exception as exc:
            logger.info("trmnl_proxy: %s fetch failed: %s", _ENDPOINTS[mode], exc)
            abort(502)

        image_url = envelope.get("image_url")
        if not isinstance(image_url, str) or not image_url:
            abort(502)
        # A self-hosted server may hand back a path rather than an
        # absolute URL; resolve it against the configured server.
        image_url = urllib.parse.urljoin(f"{base_url}/", image_url)

        # Image-bytes cache: re-use within the same TTL window. The
        # entry is keyed on (server, mode, token), so two cells that hit this
        # route within the window share one CDN fetch.
        cached = _cache_get(key)
        if cached is not None and cached["image_bytes"]:
            resp = Response(
                cached["image_bytes"], mimetype=cached["content_type"] or "image/png"
            )
            resp.headers["Cache-Control"] = "no-store"
            return resp

        try:
            body, ct = fetch_bytes(
                image_url,
                headers={"User-Agent": USER_AGENT},
                timeout=PROXY_TIMEOUT_S,
                max_bytes=IMAGE_MAX_BYTES,
                allow_local=True,
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # BlockedURLError is a ValueError, as is the size cap.
            logger.info("trmnl_proxy: image fetch failed for %s: %s", image_url, exc)
            abort(502)
        ct = ct or "image/png"

        _cache_store_image(key, body, ct)
        resp = Response(body, mimetype=ct)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return bp
