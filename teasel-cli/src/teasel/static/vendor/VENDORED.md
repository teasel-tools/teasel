# Vendored static assets

Third-party browser assets are vendored here (not pulled from a CDN at runtime) so the
web UI works offline on the bench.

## kicanvas.js

`kicanvas.js` is built from the teasel fork of kicanvas, **not** edited here.

- Source: https://github.com/teasel-tools/kicanvas (branch `teasel`)
- Built from commit: `f0886f4` (teasel-theme recolor)
- Rebuild: `git -C ../kicanvas checkout teasel && npm run build:no-check` → `build/kicanvas.js`
- Patches (on `teasel` branch):
  - KiCad 9 named-only net support + probe-only (pads/tracks/vias) selection.
    Upstreamable half lives on branch `kicad9-named-nets`.
  - UI chrome recolor: neutral slate + jade green to match teasel (kc-ui.css
    teasel-theme override block + element.ts scrollbar-color). Teasel-only.

Do not hand-edit `kicanvas.js`. Update by rebuilding the fork and re-copying.

## marked.min.js

Markdown → HTML renderer for the Journal tab. Unmodified upstream release.

- Source: https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js
- Version: marked v12.0.2 (MIT, Christopher Jeffrey)
- sha256: `15fabce5b65898b32b03f5ed25e9f891a729ad4c0d6d877110a7744aa847a894`
- Exposes the global `marked`; the Journal panel calls `marked.parse(src)`.
- Update: re-download the pinned version and refresh the hash above.
