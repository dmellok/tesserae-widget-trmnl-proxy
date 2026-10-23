# tesserae-widget-trmnl-proxy

Render whatever TRMNL is scheduling for your device on a Tesserae
cell. Install via the catalog (Settings → Widgets → Browse).

## How it works

1. Paste your TRMNL **Device API key** under
   *Settings → Widgets → TRMNL proxy*.
2. Add a cell on any page, pick **TRMNL proxy** as the widget.
3. On each refresh the host asks TRMNL for the next screen with the
   documented `Access-Token` header, parses the envelope, and paints
   the `image_url` Tesserae proxies to the panel.

The token never leaves the Tesserae host. The cell carries only the
local proxy path (`/plugins/trmnl_proxy/image`).

## Mode

The **Mode** setting picks which TRMNL endpoint the host calls.

- **Advance my playlist** (default) calls `/api/display`, the same
  call the TRMNL firmware makes. Each refresh window moves to the
  next playlist item. This is the right choice when your TRMNL
  device now points at Tesserae, or you have no TRMNL device at all,
  because nothing else is stepping the playlist.
- **Mirror my TRMNL device** calls `/api/current_screen`, which
  returns whatever the physical device last painted and never
  advances the playlist. Pick this only if a real TRMNL device still
  polls TRMNL's servers and the cell should copy it.

Upstream calls are rate-limited to one per 60 seconds per key, or
per the `refresh_rate` TRMNL returns when that is longer, however
many cells or pages use the widget. In advance mode that window is
what keeps the playlist from skipping ahead when several cells
render at different times.

## Self-hosted server (LaraPaper / BYOS)

The **Server URL** setting (default `https://trmnl.com`) points
the widget at any server that speaks the same device API, such as a
self-hosted [LaraPaper](https://github.com/usetrmnl/larapaper)
install. Enter the server's address, e.g. `http://192.168.1.20:4567`,
and paste the device API key that server issued for the device. The
widget calls `<server>/api/display` (or
`/api/current_screen` in mirror mode) with the same `Access-Token`
header TRMNL uses.

- Surrounding whitespace and trailing slashes are ignored. Only
  `http://` and `https://` URLs with a host are accepted; anything
  else shows an error on the cell and nothing is sent, so the key
  never goes to a server you didn't mean.
- **APP_URL caveat.** LaraPaper builds the `image_url` in its
  response from its `APP_URL` setting. If that is `http://localhost`
  or a hostname only the LaraPaper box can resolve, the image
  download from Tesserae fails (the Tesserae log says
  `image fetch failed`). Set `APP_URL` to an
  address Tesserae can reach. Relative image paths are resolved
  against the Server URL.
- Loopback and LAN addresses are allowed. Both the API call and the
  image download go through Tesserae's outbound URL guard, which
  still refuses link-local / cloud-metadata addresses and re-checks
  every redirect.
- Changing the Server URL (or the mode) starts a fresh cache window,
  so you never see the previous server's screen.
- TRMNL moved from usetrmnl.com to trmnl.com. Cells saved with the
  old address keep using it as typed.

## Why this exists

TRMNL's docs ([Private API → introduction](https://docs.trmnl.com/go/private-api/introduction),
API reference at [trmnl.com/api-docs.md](https://trmnl.com/api-docs.md))
explicitly endorse this use case: *"With a device's API key you can
request content without a TRMNL device or TRMNL firmware."* The user
keeps their TRMNL account, their plugin work, their compute. Tesserae
adds a downstream display surface on whatever e-ink panel they
already own.

## Limitations

- One account (and one server) per Tesserae install. Multi-account support would
  follow the same pattern as `picture_immich`; deferred until needed.
- TRMNL chooses the image dimensions based on the device kind the
  token is registered to. If the cell aspect doesn't match, the
  Pillow downscale + the `scale` option pick up the slack.
- There is no page at `/plugins/trmnl_proxy/`; the widget only
  serves `/plugins/trmnl_proxy/image`. The list of installed plugins
  is at `/plugins/`.

## Tests

From a Tesserae checkout with its venv active:

```
python -m pytest /path/to/tesserae-widget-trmnl-proxy/trmnl_proxy/tests
```

## License

AGPL-3.0-or-later.
