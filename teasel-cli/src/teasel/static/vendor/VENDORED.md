# Vendored kicanvas bundle

`kicanvas.js` is built from the teasel fork of kicanvas, **not** edited here.

- Source: https://github.com/teasel-tools/kicanvas (branch `teasel`)
- Built from commit: `f0886f4` (teasel-theme recolor)
- Rebuild: `git -C ../kicanvas checkout teasel && npm run build:no-check` → `build/kicanvas.js`
- Patches (on `teasel` branch):
  - KiCad 9 named-only net support + probe-only (pads/tracks/vias) selection.
    Upstreamable half lives on branch `kicad9-named-nets`.
  - UI chrome recolor: neutral slate + jade green to match teasel (kc-ui.css
    teasel-theme override block + element.ts scrollbar-color). Teasel-only.

Do not hand-edit this file. Update by rebuilding the fork and re-copying.
