# Vendored kicanvas bundle

`kicanvas.js` is built from the teasel fork of kicanvas, **not** edited here.

- Source: https://github.com/teasel-tools/kicanvas (branch `teasel`)
- Built from commit: `ff90146` (on `upstream/main` @ `b031159`, theacodes/kicanvas)
- Rebuild: `git -C ../kicanvas checkout teasel && npm run build:no-check` → `build/kicanvas.js`
- Patch: KiCad 9 named-only net support + probe-only (pads/tracks/vias) selection.
  Upstreamable half lives on branch `kicad9-named-nets`.

Do not hand-edit this file. Update by rebuilding the fork and re-copying.
