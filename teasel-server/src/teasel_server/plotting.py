"""House-styled plotting tools — the paved road for datasheet-consistent figures.

Every figure teasel produces goes through one shared .mplstyle (slate + jade,
matching the web UI), so a time trace, an overlay, and a Bode plot made weeks
apart all look like siblings. The `plot` tool renders the common layouts from a
small declarative config + named presets; `get_plot_style` hands the style path
to free-handed matplotlib code so exotic one-offs still match the house look.

Cross-cutting (any trace, any source), so these are top-level tools registered
from main() — not a driver.
"""

import csv
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path

import tomllib

from .output import output_dir, write_waveform

_TEASEL_DIR = Path.home() / ".teasel"
_PRESETS_PATH = _TEASEL_DIR / "plots.toml"

# Named styles: <name>.mplstyle files. Bundled ones ship with the package; the
# user's editable copies live in ~/.teasel/styles/ and override by name. "dark"
# is the default; a project selects another via project.toml [plot].style.
_STYLE_DIR = _TEASEL_DIR / "styles"
_BUNDLED_STYLE_DIR = Path(__file__).parent / "styles"
_DEFAULT_STYLE = "dark"

# Built-in presets; user presets in ~/.teasel/plots.toml override by name.
_BUILTIN_PRESETS: dict[str, dict] = {
    "time": {"kind": "time", "x_label": "Time", "y_label": "Voltage"},
    "bode": {"kind": "bode", "x_label": "Frequency", "y_label": "Magnitude (dB)"},
    "xy": {"kind": "xy"},
}

_CONFIG_KEYS = ("kind", "title", "x_label", "y_label", "x_scale", "y_scale",
                "x_unit", "y_unit", "legend", "grid")


def ensure_styles() -> Path:
    """Seed ~/.teasel/styles/<name>.mplstyle from the bundled styles on first use.

    Each user copy is the live one (editable to restyle that style everywhere at
    once); we never overwrite an existing copy. Returns the styles directory."""
    _STYLE_DIR.mkdir(parents=True, exist_ok=True)
    for src in _BUNDLED_STYLE_DIR.glob("*.mplstyle"):
        dest = _STYLE_DIR / src.name
        if not dest.exists():
            dest.write_text(src.read_text())
    return _STYLE_DIR


def list_styles() -> dict[str, Path]:
    """All known styles, name -> path. Bundled styles plus ~/.teasel/styles/;
    a user copy overrides a bundled one of the same name."""
    ensure_styles()
    out: dict[str, Path] = {s.stem: s for s in _BUNDLED_STYLE_DIR.glob("*.mplstyle")}
    out.update({s.stem: s for s in _STYLE_DIR.glob("*.mplstyle")})
    return out


def resolve_style(spec: str | None, project_dir: Path | None = None) -> Path:
    """Resolve a style spec to an .mplstyle path.

    ``spec`` is either a style *name* ('dark', 'datasheet', or any name in
    ~/.teasel/styles/), or a *path* to a project-local .mplstyle (absolute, or
    relative to ``project_dir``). Empty/None or an unresolvable spec falls back
    to the default style ('dark')."""
    styles = list_styles()
    if spec:
        spec = spec.strip()
        looks_like_path = spec.endswith(".mplstyle") or "/" in spec or os.sep in spec
        if looks_like_path:
            p = Path(spec)
            if not p.is_absolute() and project_dir is not None:
                p = project_dir / p
            if p.exists():
                return p
        name = Path(spec).stem  # tolerate "datasheet.mplstyle" given as a name
        if name in styles:
            return styles[name]
    return styles.get(_DEFAULT_STYLE) or next(iter(styles.values()))


def _dump_presets_toml(presets: dict[str, dict]) -> str:
    """Serialize presets to TOML by hand — configs are flat str/bool/number
    tables, which keeps stdlib-only (tomllib can't write)."""
    lines: list[str] = []
    for name, cfg in sorted(presets.items()):
        lines.append(f"[presets.{json.dumps(name)}]" if not re.fullmatch(r"[A-Za-z0-9_-]+", name)
                     else f"[presets.{name}]")
        for k, v in cfg.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {str(v).lower()}")
            elif isinstance(v, (int, float)):
                lines.append(f"{k} = {v}")
            else:
                lines.append(f"{k} = {json.dumps(str(v))}")  # JSON string ≈ TOML basic string
        lines.append("")
    return "\n".join(lines)


def _load_user_presets() -> dict[str, dict]:
    if not _PRESETS_PATH.exists():
        return {}
    try:
        return tomllib.loads(_PRESETS_PATH.read_text()).get("presets", {})
    except Exception:
        return {}


def _all_presets() -> dict[str, dict]:
    presets = dict(_BUILTIN_PRESETS)
    presets.update(_load_user_presets())
    return presets


def _resolve_config(preset: str, overrides: dict) -> dict:
    """built-in defaults → preset → inline overrides (later wins)."""
    cfg: dict = {"kind": "time", "legend": True, "grid": True}
    if preset:
        presets = _all_presets()
        if preset not in presets:
            raise RuntimeError(
                f"Unknown preset '{preset}'. Available: {', '.join(sorted(presets))}"
            )
        cfg.update(presets[preset])
    cfg.update({k: v for k, v in overrides.items() if v not in ("", None)})
    return cfg


# ── trace loading ─────────────────────────────────────────────────────────────

def _read_csv(path: Path) -> tuple[list[float], list[float]]:
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise RuntimeError(f"Empty CSV: {path}")
    body = rows[1:] if rows and not _is_number(rows[0][0]) else rows
    x = [float(r[0]) for r in body]
    y = [float(r[1]) for r in body]
    return x, y


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _load_trace(spec: dict, get_drivers) -> tuple[list[float], list[float], str]:
    """Resolve one trace spec to (x, y, label).

    {"csv": path} — a waveform CSV (bench capture or saved sim). Relative paths
    resolve against the working directory first (tool results reference e.g.
    "teasel-output/x.csv"), then bare filenames against the output dir.
    {"sim": node} — run the SPICE simulator now.
    """
    if "csv" in spec:
        path = Path(spec["csv"])
        if not path.is_absolute() and not path.exists():
            path = output_dir() / path
        if not path.exists():
            raise RuntimeError(f"CSV not found: {spec['csv']}")
        x, y = _read_csv(path)
        return x, y, spec.get("label") or path.stem
    if "sim" in spec:
        sim = next((d for d in get_drivers() if getattr(d, "slug", "") == "spice"), None)
        if sim is None:
            raise RuntimeError("No SPICE simulator loaded.")
        data = sim.simulate(spec["sim"], spec.get("analysis", "tran"))
        write_waveform(data)  # keep the data behind the figure reproducible
        return data.time_data, data.voltage_data, spec.get("label") or f"sim {spec['sim']}"
    raise RuntimeError(f"Trace spec needs 'csv' or 'sim': {json.dumps(spec)}")


# ── rendering ─────────────────────────────────────────────────────────────────

def _render(traces: list[tuple[list[float], list[float], str]], cfg: dict,
            style_path: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import EngFormatter

    plt.style.use(str(style_path))
    fig, ax = plt.subplots()
    kind = cfg.get("kind", "time")

    for x, y, label in traces:
        if kind == "bode":
            y = [20 * math.log10(abs(v)) if v else -240.0 for v in y]
        ax.plot(x, y, label=label)

    if kind == "bode" or cfg.get("x_scale") == "log":
        ax.set_xscale("log")
    if cfg.get("y_scale") == "log":
        ax.set_yscale("log")

    # engineering-notation ticks (k/M/µ) on linear axes; units ride the ticks
    x_unit = cfg.get("x_unit", {"time": "s", "bode": "Hz"}.get(kind, ""))
    y_unit = cfg.get("y_unit", "V" if kind == "time" else "")
    if ax.get_xscale() == "linear":
        ax.xaxis.set_major_formatter(EngFormatter(unit=x_unit, sep="\N{THIN SPACE}"))
    if ax.get_yscale() == "linear" and y_unit:
        ax.yaxis.set_major_formatter(EngFormatter(unit=y_unit, sep="\N{THIN SPACE}"))

    if cfg.get("x_label"):
        ax.set_xlabel(cfg["x_label"])
    if cfg.get("y_label"):
        ax.set_ylabel(cfg["y_label"])
    if cfg.get("title"):
        ax.set_title(cfg["title"])
    ax.grid(bool(cfg.get("grid", True)))
    if cfg.get("legend", True) and len(traces) > 1:
        ax.legend()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:21]
    svg = output_dir() / f"plot_{ts}.svg"
    fig.savefig(svg)
    fig.savefig(svg.with_suffix(".png"))
    plt.close(fig)
    return svg


# ── tool registration ─────────────────────────────────────────────────────────

def register_plot_tools(mcp, get_drivers, get_project_style=None) -> None:
    """Register the plotting tools on the MCP server. `get_drivers` returns the
    live driver list (used to resolve {"sim": node} trace specs). `get_project_style`
    returns ``(style_spec, project_dir)`` for the active project — the project's
    chosen style (project.toml [plot].style) and the dir its relative style paths
    resolve against; defaults to ("", None) → the default 'dark' style."""

    if get_project_style is None:
        get_project_style = lambda: ("", None)

    def plot(traces: list[dict], preset: str = "", kind: str = "", title: str = "",
             x_label: str = "", y_label: str = "", x_scale: str = "",
             y_scale: str = "", style: str = "") -> str:
        """Render a house-styled plot (consistent datasheet look) to SVG + PNG.

        traces: list of trace specs — {"csv": "<path or output filename>", "label": "..."}
        for a saved waveform (bench capture or sim CSV), or {"sim": "<node>",
        "analysis": "tran"|"ac", "label": "..."} to simulate a node now. Mix freely
        to overlay sim vs measurement.
        preset: named config ("time", "bode", "xy", or a saved preset).
        kind: "time" (linear, V/s engineering ticks), "bode" (log-f, dB magnitude
        — pair with sim analysis "ac"), or "xy". Other args override the preset.
        style: one-off visual style ("dark", "datasheet", …) overriding the
        project's [plot].style for this plot only; default uses the project's
        style (or 'dark'). See list_plot_styles.
        Returns the saved SVG/PNG paths and the resolved config (reproducible)."""
        try:
            if not traces:
                return "Error: no traces given."
            cfg = _resolve_config(preset, {
                "kind": kind, "title": title, "x_label": x_label,
                "y_label": y_label, "x_scale": x_scale, "y_scale": y_scale,
            })
            project_style, project_dir = get_project_style()
            style_path = resolve_style(style or project_style, project_dir)
            loaded = [_load_trace(t, get_drivers) for t in traces]
            svg = _render(loaded, cfg, style_path)
            labels = ", ".join(lbl for _, _, lbl in loaded)
            return (
                f"Plot saved to {svg} (and {svg.with_suffix('.png').name}). "
                f"{len(loaded)} trace(s): {labels}. Style: {style_path.stem}.\n"
                f"Resolved config: {json.dumps(cfg)}"
            )
        except Exception as e:
            return f"Error: {e}"

    def list_plot_presets() -> str:
        """List available plot presets (built-in + user presets from ~/.teasel/plots.toml)."""
        user = _load_user_presets()
        lines = []
        for name, cfg in sorted(_all_presets().items()):
            origin = "user" if name in user else "built-in"
            lines.append(f"{name} ({origin}): {json.dumps(cfg)}")
        return "\n".join(lines)

    def save_plot_preset(name: str, config: dict) -> str:
        """Save a named plot preset to ~/.teasel/plots.toml for reuse via plot(preset=name).

        config keys: kind ("time"|"bode"|"xy"), title, x_label, y_label,
        x_scale/y_scale ("lin"|"log"), x_unit, y_unit, legend, grid."""
        try:
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
                return "Error: preset name must be lowercase letters/digits/hyphens/underscores."
            bad = [k for k in config if k not in _CONFIG_KEYS]
            if bad:
                return f"Error: unknown config key(s) {bad}. Valid: {list(_CONFIG_KEYS)}"
            _TEASEL_DIR.mkdir(exist_ok=True)
            # Parse strictly here (unlike the read path's graceful fallback):
            # rewriting on top of an unparseable file would silently discard
            # every preset the user has saved.
            presets: dict[str, dict] = {}
            if _PRESETS_PATH.exists():
                try:
                    presets = tomllib.loads(_PRESETS_PATH.read_text()).get("presets", {})
                except Exception as e:
                    return (f"Error: {_PRESETS_PATH} exists but does not parse ({e}) — "
                            "fix or remove it; refusing to overwrite your saved presets.")
            presets[name] = config
            _PRESETS_PATH.write_text(_dump_presets_toml(presets))
            return f"Preset '{name}' saved to {_PRESETS_PATH}."
        except Exception as e:
            return f"Error: {e}"

    def list_plot_styles() -> str:
        """List available plot styles (built-in + ~/.teasel/styles/*.mplstyle) and
        mark the one the current project uses. Set a project's style in its
        project.toml:  [plot] style = "datasheet"  (a name) or
        style = "mystyle.mplstyle"  (a project-local file the project ships)."""
        styles = list_styles()
        spec, pdir = get_project_style()
        active = resolve_style(spec, pdir).stem
        user = {s.stem for s in _STYLE_DIR.glob("*.mplstyle")}
        lines = []
        for name in sorted(styles):
            origin = "user" if name in user else "built-in"
            mark = "  <- active" if name == active else ""
            lines.append(f"{name} ({origin}){mark}")
        if spec and active != Path(spec).stem:
            lines.append(f"(project requested '{spec}', unresolved — using '{active}')")
        return "\n".join(lines)

    def get_plot_style(name: str = "") -> str:
        """Path to a teasel .mplstyle, for hand-written matplotlib (apply with
        plt.style.use(<this path>) so the figure matches every other teasel plot).
        With no name, returns the style the current project uses (its [plot].style,
        else the default 'dark'); pass a name ('dark', 'datasheet', …) for a
        specific one. Note: the engineering tick formatter (k/M/µ) is applied by
        the plot tool, not the style — use matplotlib.ticker.EngFormatter for that
        in hand-written code."""
        if name:
            return str(resolve_style(name))
        spec, pdir = get_project_style()
        return str(resolve_style(spec, pdir))

    mcp.add_tool(plot, name="plot")
    mcp.add_tool(list_plot_presets, name="list_plot_presets")
    mcp.add_tool(save_plot_preset, name="save_plot_preset")
    mcp.add_tool(list_plot_styles, name="list_plot_styles")
    mcp.add_tool(get_plot_style, name="get_plot_style")
