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

## Why this exists

TRMNL's docs ([Private API → introduction](https://docs.trmnl.com/go/private-api/introduction))
explicitly endorse this use case: *"With a device's API key you can
request content without a TRMNL device or TRMNL firmware."* The user
keeps their TRMNL account, their plugin work, their compute. Tesserae
adds a downstream display surface on whatever e-ink panel they
already own.

## Limitations

- One account per Tesserae install. Multi-account support would
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
