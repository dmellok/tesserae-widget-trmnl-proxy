// trmnl_proxy, full-bleed render of TRMNL's next-up screen.
// Same shape as picture_gallery's client: ``(shadow, ctx)``.

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function objectFitFor(scale) {
  switch (scale) {
    case "fill": return "cover";
    case "center": return "none";
    case "stretch": return "fill";
    case "fit":
    default: return "contain";
  }
}

export default function render(shadow, ctx) {
  const data = ctx?.data ?? {};
  const opts = ctx?.cell?.options || {};
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;

  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="trmnl_proxy">
        <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>TRMNL</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  if (!data.image_url) {
    shadow.innerHTML = `
      ${css}
      <div class="w is-bleed" data-widget="trmnl_proxy">
        <div class="bleed-empty">No screen yet.</div>
      </div>`;
    return;
  }

  const fit = objectFitFor(data.scale || opts.scale || "fit");
  // Append the TRMNL filename as a query param so the browser
  // re-fetches when the screen changes; same trick picture_gallery
  // uses for upload busts.
  const url = data.image_query_filename
    ? `${data.image_url}?f=${encodeURIComponent(data.image_query_filename)}`
    : data.image_url;

  // ``object-fit`` lives on the inline ``style`` attribute so it
  // outranks ``spectra-widgets.css`` ``.w.is-bleed > img``, which
  // otherwise hardcodes ``cover`` and ignores the cell option.
  shadow.innerHTML = `
    ${css}
    <div class="w is-bleed" data-widget="trmnl_proxy">
      <img src="${escapeHtml(url)}" alt="" loading="eager"
           style="width:100%;height:100%;display:block;object-fit:${fit};object-position:center;">
    </div>`;
}
