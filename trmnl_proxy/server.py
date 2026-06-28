"""trmnl_proxy, render the next-up TRMNL screen on a Tesserae cell.

Single-account plugin: one ``access_token`` in plugin settings drives
every cell. ``fetch()`` produces a same-origin proxy URL that carries
the cell's pixel dims as query params. The blueprint's ``/image``
route picks those up, calls ``GET /api/current_screen`` with the
matching ``Width`` / ``Height`` / ``png-width`` / ``png-height``
headers, and serves the resulting image bytes back to the cell.

``/api/current_screen`` mirrors whatever's currently on the device,
doesn't advance the playlist. Per TRMNL's docs it's designed for
"BYOD devices that mirror official device content"; the
explicit caveat in the docs is "designed for our Chrome extension,
please don't abuse it". We rate-limit ourselves to at most one
upstream call per ``RATE_LIMIT_S`` (currently 60s) per token by
caching the envelope + the image bytes in process memory.

The proxy hop exists for two reasons:

* **Mixed-content safety on mobile Safari.** Tesserae is served over
  HTTP on the LAN; TRMNL's image CDN is HTTPS. Desktop browsers
  allow HTTPS subresources on HTTP pages; iOS Safari blocks them
  in some configurations. Same-origin proxy sidesteps it.
* **Token hygiene.** The cell never holds the access token in any
  URL it builds. Only the host-side ``/api/current_screen`` call
  carries it.

Notes for the private-testing scope:

* No multi-account admin pages. Tesserae's standard settings form
  renders the ``access_token`` field automatically thanks to the
  manifest declaration; nothing custom needed.
* No catalog publish, no GitHub push. Lives in a sibling repo + a
  symlink under ``data/marketplace/``.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from flask import Blueprint, Response, abort, current_app, request

from app.plugin_http import fetch_json

logger = logging.getLogger(__name__)

TRMNL_BASE = "https://usetrmnl.com"
# ``/api/current_screen`` mirrors whatever's currently showing on the
# device tied to this token, without advancing the playlist. Per
# TRMNL's docs it's the documented BYOD path for "mirror what my
# device is showing right now". ``/api/display`` would also work but
# every call advances the playlist by one slot, which produces noisy
# rotation when multiple Tesserae cells render at different times.
TRMNL_DISPLAY = "/api/current_screen"
HTTP_TIMEOUT_S = 12
PROXY_TIMEOUT_S = 20
# Floor on time between upstream calls. TRMNL's docs explicitly ask
# callers not to abuse current_screen; this rate-limits us per token
# regardless of how many cells refresh at what cadence. The envelope's
# own ``refresh_rate`` value extends this when it's larger.
RATE_LIMIT_S = 60


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
# token -> {expires_at, envelope, image_bytes, content_type}. ``None``
# values mean "envelope cached but image not yet fetched". One entry
# per token; multiple cells with the same token share state.
_CACHE: dict[str, dict[str, Any]] = {}


def _cache_get(token: str) -> dict[str, Any] | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(token)
        if entry is None:
            return None
        if entry["expires_at"] <= time.time():
            return None
        return entry


def _cache_store_envelope(token: str, envelope: dict[str, Any]) -> dict[str, Any]:
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
        _CACHE[token] = entry
    return entry


def _cache_store_image(token: str, body: bytes, content_type: str) -> None:
    with _CACHE_LOCK:
        entry = _CACHE.get(token)
        if entry is not None:
            entry["image_bytes"] = body
            entry["content_type"] = content_type


def _stale_entry(token: str) -> dict[str, Any] | None:
    """Return the cached entry even if expired. Used after a 429 / 409
    rate-limit response so the panel keeps painting the last good
    frame instead of erroring."""
    with _CACHE_LOCK:
        return _CACHE.get(token)


def _fetch_current_screen(token: str, cell_w: int, cell_h: int) -> dict[str, Any]:
    """Call ``/api/current_screen`` and return the parsed envelope.
    Respects the in-process cache; populates it on success. On HTTP
    429 / 409 (rate limit), returns the stale envelope when one
    exists, else raises."""
    cached = _cache_get(token)
    if cached is not None:
        return cached["envelope"]
    headers = {
        "Access-Token": token,
        "Accept": "application/json",
        "User-Agent": "tesserae-trmnl-proxy/0.3",
        **_dim_headers(cell_w, cell_h),
    }
    try:
        envelope = fetch_json(
            f"{TRMNL_BASE}{TRMNL_DISPLAY}",
            headers=headers,
            timeout=HTTP_TIMEOUT_S,
            retries=0,
        )
    except urllib.error.HTTPError as exc:
        # TRMNL surfaces "too fast" via 429 (standard) but 409 has
        # also been observed. Either way, fall back to whatever we
        # last cached for this token, even if formally expired.
        if exc.code in (409, 429):
            stale = _stale_entry(token)
            if stale is not None and stale.get("envelope"):
                logger.info(
                    "trmnl_proxy: rate-limited by upstream (HTTP %s); serving stale envelope",
                    exc.code,
                )
                return stale["envelope"]
        raise
    if not isinstance(envelope, dict):
        raise ValueError("TRMNL returned a non-dict envelope")
    _cache_store_envelope(token, envelope)
    return envelope


# ----- plugin contract ------------------------------------------------


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    """Resolve the next-up TRMNL screen and return a same-origin proxy
    URL the cell client paints. The actual ``/api/current_screen``
    round-trip happens once per ``RATE_LIMIT_S`` window per token,
    cached across cells."""
    del settings
    token = _access_token()
    if not token:
        return {
            "error": "Paste a TRMNL device API key under Settings → Widgets → TRMNL proxy.",
        }

    cell_w = int(ctx.get("cell_w") or 0)
    cell_h = int(ctx.get("cell_h") or 0)

    try:
        envelope = _fetch_current_screen(token, cell_w, cell_h)
    except Exception as exc:
        return {"error": f"TRMNL API failure: {type(exc).__name__}: {exc}"}

    status = envelope.get("status")
    # ``current_screen`` returns 200 on success. ``display`` returned 0.
    # Treat either as fine; only flag explicit error statuses.
    if isinstance(status, int) and status >= 400:
        return {
            "error": (
                f"TRMNL said status {status}. "
                f"Check the device on the TRMNL dashboard."
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

        try:
            cell_w = int(request.args.get("w") or 0)
            cell_h = int(request.args.get("h") or 0)
        except ValueError:
            cell_w = cell_h = 0

        try:
            envelope = _fetch_current_screen(token, cell_w, cell_h)
        except Exception as exc:
            logger.info("trmnl_proxy: api/current_screen fetch failed: %s", exc)
            abort(502)

        image_url = envelope.get("image_url")
        if not isinstance(image_url, str) or not image_url:
            abort(502)

        # Image-bytes cache: re-use within the same TTL window. The
        # entry is keyed on token, so two cells that hit this route
        # within the window share one CDN fetch.
        cached = _cache_get(token)
        if cached is not None and cached["image_bytes"]:
            resp = Response(cached["image_bytes"], mimetype=cached["content_type"] or "image/png")
            resp.headers["Cache-Control"] = "no-store"
            return resp

        req = urllib.request.Request(image_url, headers={"User-Agent": "tesserae-trmnl-proxy/0.3"})
        try:
            with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT_S) as upstream:
                body = upstream.read()
                ct = upstream.headers.get("Content-Type") or "image/png"
        except (urllib.error.URLError, OSError) as exc:
            logger.info("trmnl_proxy: image fetch failed for %s: %s", image_url, exc)
            abort(502)

        _cache_store_image(token, body, ct)
        resp = Response(body, mimetype=ct)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return bp
