# tesserae-widget-trmnl-proxy

Render whatever TRMNL is scheduling for your device on a Tesserae
cell. Install via the catalog (Settings → Widgets → Browse).

## How it works

1. Paste your TRMNL **Device API key** under
   *Settings → Widgets → TRMNL proxy*.
2. Add a cell on any page, pick **TRMNL proxy** as the widget.
3. The cell calls `https://usetrmnl.com/api/display` server-side with
   the documented `Access-Token` header, parses the BYOS envelope,
   and paints the `image_url` Tesserae proxies to the panel.

The token never leaves the Tesserae host. The cell carries only the
local proxy path (`/plugins/trmnl_proxy/image`).

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

## License

AGPL-3.0-or-later.
