"""Unified MCP server — discovers instrument drivers via entry points and registers tools."""

import functools
import logging
import os
import tomllib
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Callable

from mcp.server.fastmcp import FastMCP

logging.getLogger("pyvisa").setLevel(logging.CRITICAL)

_log = logging.getLogger("teasel")
_log_file = Path.home() / ".teasel" / "server.log"
_log_file.parent.mkdir(exist_ok=True)
logging.basicConfig(
    filename=_log_file,
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from .base import FunctionGeneratorBase, InstrumentBase, OscilloscopeBase, SimulatorBase
from .llm import LLMClient
from .output import write_screenshot, write_waveform

CF_DEFAULT_MODEL = "@cf/mistralai/mistral-small-3.1-24b-instruct"
CF_BASE_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"

mcp = FastMCP("teasel")  # reconfigured in main() before use


# ── Server state ──────────────────────────────────────────────────────────────

@dataclass
class _ServerState:
    setup_path: Path | None = None
    setup_mtime: float = 0.0
    setup_cache: dict = field(default_factory=dict)
    setup_changed: bool = False
    setup_diff: str = ""
    drivers: list[InstrumentBase] = field(default_factory=list)
    llm: LLMClient | None = None
    ui_url: str = ""  # web UI base URL for best-effort outbound cues (show_probe_point)


_state = _ServerState()


def _push_ui_event(event: dict) -> None:
    """Best-effort fire-and-forget POST to the web UI's /ui/event. No-op on any
    failure (UI not running, headless CI). Runs in a daemon thread so the 1s
    timeout never stalls a tool return (stdio) or the event loop (streamable-http).
    Never raises into a tool call."""
    if not _state.ui_url:
        return
    import json
    import threading
    import urllib.request

    def _post():
        try:
            req = urllib.request.Request(
                f"{_state.ui_url.rstrip('/')}/ui/event",
                data=json.dumps(event).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=1.0).close()
        except Exception as e:  # UI down / unreachable — cue is optional
            _log.debug("UI push failed (%s): %s", event.get("type"), e)

    threading.Thread(target=_post, daemon=True).start()


# ── Setup-change detection ────────────────────────────────────────────────────

def _check_setup() -> None:
    """Poll setup.toml mtime; on change reload limits and set the pending-change flag."""
    if _state.setup_path is None or not _state.setup_path.exists():
        return
    try:
        mtime = _state.setup_path.stat().st_mtime
    except OSError:
        return
    if mtime == _state.setup_mtime:
        return
    _state.setup_mtime = mtime
    try:
        new_setup = tomllib.loads(_state.setup_path.read_text())
    except Exception:
        return
    old_setup = _state.setup_cache
    _state.setup_cache = new_setup
    _apply_setup(new_setup)
    _state.setup_diff = _diff_setup(old_setup, new_setup)
    _state.setup_changed = True


# ── Physical safety limits ────────────────────────────────────────────────────
#
# project.toml [instruments.<name>.limits] is the operator's hard ceiling for what
# a stimulus instrument may be told to do to the device under test. It is enforced
# HERE, server-side, before any command reaches the driver — the model never gets
# to negotiate with it. This is distinct from the brain's *reliability* guardrails
# (retry/rescue nudges): those keep the model coherent; these keep the hardware safe.
#
# Numeric keys (units in the key name):
#   amplitude_max  — peak-to-peak output voltage (Vpp)
#   voltage_max    — absolute peak output voltage, |dc| + vpp/2 (V) — a DUT's
#                    absolute-maximum rating is a peak, so this is DC-offset aware
#   frequency_max  — output frequency (Hz)
# String keys:
#   reason         — free text saying *why* (e.g. "TLE2142 input abs-max ±15 V");
#                    surfaced in every refusal and in the tool descriptions.

_LIMIT_TEXT_KEYS = ("reason",)


def _parse_limits(data: dict) -> dict:
    """[limits] table → {key: float | str}. Non-numeric values for numeric keys are
    dropped (a typo must fail open loudly in the log, not crash the server)."""
    out: dict = {}
    for k, v in (data or {}).items():
        if k in _LIMIT_TEXT_KEYS:
            out[k] = str(v)
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            _log.warning("Ignoring non-numeric limit %s = %r", k, v)
    return out


def _limit_note(d) -> str:
    """One-line description of a driver's active limits, for tool docstrings."""
    limits = getattr(d, "_limits", {}) or {}
    parts = []
    if "amplitude_max" in limits:
        parts.append(f"amplitude ≤ {limits['amplitude_max']:g} Vpp")
    if "voltage_max" in limits:
        parts.append(f"peak |V| ≤ {limits['voltage_max']:g} V (DC offset included)")
    if "frequency_max" in limits:
        parts.append(f"frequency ≤ {limits['frequency_max']:g} Hz")
    if not parts:
        return ""
    note = " SAFETY LIMITS (enforced by teasel, cannot be overridden by you): " + ", ".join(parts) + "."
    if limits.get("reason"):
        note += f" Reason: {limits['reason']}."
    return note


def _fg_violation(d, freq_hz: float | None = None, vpp: float | None = None,
                  dc: float | None = None) -> str | None:
    """Return a refusal message if the requested settings exceed the driver's
    limits, else None. Checked before the driver is touched."""
    limits = getattr(d, "_limits", {}) or {}
    if not limits:
        return None
    name = getattr(d, "instance_name", d.slug)
    problems = []
    if freq_hz is not None and "frequency_max" in limits and freq_hz > limits["frequency_max"]:
        problems.append(f"{freq_hz:g} Hz exceeds frequency_max = {limits['frequency_max']:g} Hz")
    if vpp is not None and "amplitude_max" in limits and vpp > limits["amplitude_max"]:
        problems.append(f"{vpp:g} Vpp exceeds amplitude_max = {limits['amplitude_max']:g} Vpp")
    if vpp is not None and "voltage_max" in limits:
        peak = abs(dc or 0.0) + vpp / 2.0
        if peak > limits["voltage_max"]:
            problems.append(
                f"peak |V| = |{dc or 0.0:g} V DC| + {vpp:g} Vpp / 2 = {peak:g} V exceeds "
                f"voltage_max = {limits['voltage_max']:g} V"
            )
    if not problems:
        return None
    why = f" Reason: {limits['reason']}." if limits.get("reason") else ""
    return (
        f"REFUSED by teasel safety limit — nothing was sent to {name}. "
        + "; ".join(problems) + f".{why} "
        f"These limits are set by the operator in project.toml "
        f"[instruments.{name}.limits] and cannot be overridden from here; tell the "
        f"user what you wanted to do and why it was refused, and offer a value within the limit."
    )


def _apply_setup(setup_config: dict) -> None:
    """Push updated limits from setup_config into all running drivers."""
    for driver in _state.drivers:
        inst_name = getattr(driver, "instance_name", driver.slug)
        data = setup_config.get("instruments", {}).get(inst_name, {})
        driver._limits = _parse_limits(data.get("limits", {}))


def _diff_setup(old: dict, new: dict) -> str:
    """Return a short human-readable summary of what changed between two setup dicts."""
    parts: list[str] = []
    old_insts = old.get("instruments", {})
    new_insts = new.get("instruments", {})
    for slug in set(new_insts) | set(old_insts):
        old_s = old_insts.get(slug, {})
        new_s = new_insts.get(slug, {})
        for k in set(old_s.get("limits", {})) | set(new_s.get("limits", {})):
            ov = old_s.get("limits", {}).get(k)
            nv = new_s.get("limits", {}).get(k)
            if ov != nv:
                parts.append(f"{slug}.{k}: {ov} → {nv}")
        for ch in set(old_s.get("channels", {})) | set(new_s.get("channels", {})):
            old_cfg = old_s.get("channels", {}).get(ch, {})
            new_cfg = new_s.get("channels", {}).get(ch, {})
            for f in set(old_cfg) | set(new_cfg):
                ov, nv = old_cfg.get(f), new_cfg.get(f)
                if ov != nv:
                    parts.append(f"{slug}.{ch}.{f}: {ov} → {nv}")

    # Probe map ([boards.*.probes]): "what is each channel probing now". The
    # operator re-pointing a probe in the web UI lands here — this is the human's
    # half of the loop, so summarize it as clearly as instrument changes.
    def _by_channel(board_cfg: dict) -> dict:
        out: dict = {}
        for p in board_cfg.get("probes", []):
            out[(p.get("instrument", ""), p.get("channel", ""))] = p
        return out

    old_boards = old.get("boards", {})
    new_boards = new.get("boards", {})
    for board in set(old_boards) | set(new_boards):
        ob = _by_channel(old_boards.get(board, {}))
        nb = _by_channel(new_boards.get(board, {}))
        for inst, ch in set(ob) | set(nb):
            op, np_ = ob.get((inst, ch)), nb.get((inst, ch))
            on = op.get("net") if op else None  # project.toml stores the net as "net"
            nn = np_.get("net") if np_ else None
            where = f"{inst}.{ch}" if inst else ch
            if on != nn:
                parts.append(f"probe {where}: {on or '(none)'} → {nn or '(none)'}")
            elif op and np_ and op.get("role") != np_.get("role"):
                parts.append(f"probe {where} role: {op.get('role')} → {np_.get('role')}")
    return "; ".join(parts)


def with_setup_check(fn: Callable) -> Callable:
    """Decorator: if setup.toml changed since last get_setup call, prepend a warning."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        _check_setup()
        result = fn(*args, **kwargs)
        if _state.setup_changed:
            return f"⚠ setup changed — call get_setup to see what changed\n\n{result}"
        return result
    return wrapper


# ── Tool builders ─────────────────────────────────────────────────────────────

def _register_function_generator(driver: FunctionGeneratorBase) -> None:
    slug = getattr(driver, "instance_name", driver.slug)

    # _limits is an optional driver convention, not part of FunctionGeneratorBase:
    # the parsed project.toml [limits] table (see _parse_limits). Drivers without
    # one behave as unlimited. Every setter runs _fg_violation BEFORE touching the
    # driver, so a refused request never reaches the instrument.
    def make_set_frequency(d):
        def set_frequency(freq_hz: float) -> str:
            if (refusal := _fg_violation(d, freq_hz=freq_hz)):
                return refusal
            try:
                d.set_frequency(freq_hz)
                return f"Frequency set to {freq_hz} Hz"
            except Exception as e:
                return f"Error: {e}"
        set_frequency.__doc__ = f"Set output frequency on {d.name}. Args: freq_hz: Frequency in Hz.{_limit_note(d)}"
        return set_frequency

    def make_set_amplitude(d):
        def set_amplitude(vpp: float, dc: float = 0.0) -> str:
            if (refusal := _fg_violation(d, vpp=vpp, dc=dc)):
                return refusal
            try:
                d.set_amplitude(vpp, dc)
                return f"Amplitude set to {vpp} Vpp, DC offset {dc} V"
            except Exception as e:
                return f"Error: {e}"
        set_amplitude.__doc__ = f"Set output amplitude on {d.name}. Args: vpp: Peak-to-peak voltage. dc: DC offset in volts.{_limit_note(d)}"
        return set_amplitude

    def make_set_waveform(d):
        def set_waveform(waveform: str) -> str:
            try:
                d.set_waveform(waveform)
                return f"Waveform set to {waveform}"
            except Exception as e:
                return f"Error: {e}"
        set_waveform.__doc__ = f"Set output waveform on {d.name}. Args: waveform: One of {d.waveforms}."
        return set_waveform

    def make_configure(d):
        def configure(freq_hz: float, vpp: float, dc: float = 0.0, waveform: str = "sine") -> str:
            if (refusal := _fg_violation(d, freq_hz=freq_hz, vpp=vpp, dc=dc)):
                return refusal
            try:
                d.configure(freq_hz, vpp, dc, waveform)
                return f"Configured: {freq_hz} Hz, {vpp} Vpp, {dc} V DC, {waveform}"
            except Exception as e:
                return f"Error: {e}"
        configure.__doc__ = f"Set frequency, amplitude, DC offset, and waveform in one call on {d.name}.{_limit_note(d)}"
        return configure

    mcp.add_tool(with_setup_check(make_set_frequency(driver)), name=f"{slug}_set_frequency")
    mcp.add_tool(with_setup_check(make_set_amplitude(driver)), name=f"{slug}_set_amplitude")
    mcp.add_tool(with_setup_check(make_set_waveform(driver)), name=f"{slug}_set_waveform")
    mcp.add_tool(with_setup_check(make_configure(driver)), name=f"{slug}_configure")


def _register_oscilloscope(driver: OscilloscopeBase) -> None:
    slug = getattr(driver, "instance_name", driver.slug)

    def make_capture_waveform(d):
        def capture_waveform(channel: int) -> str:
            try:
                data = d.capture_waveform(channel)
                path = write_waveform(data)
                n = len(data.voltage_data)
                v_min = min(data.voltage_data)
                v_max = max(data.voltage_data)
                v_mean = sum(data.voltage_data) / n
                vpp = v_max - v_min
                if _state.llm:
                    try:
                        summary = _state.llm.summarize_waveform({
                            "channel": channel, "points": n,
                            "sample_rate_hz": data.sample_rate,
                            "v_min": v_min, "v_max": v_max, "v_mean": v_mean,
                        })
                        return f"{summary}\nData saved to {path}"
                    except Exception as e:
                        _log.warning(f"LLM waveform summary failed: {e}")
                return (
                    f"Ch{channel}: {vpp:.3f}Vpp, mean {v_mean:.3f}V, "
                    f"{n} pts @ {data.sample_rate/1e3:.0f}kHz SR. Saved to {path}"
                )
            except Exception as e:
                return f"Error: {e}"
        capture_waveform.__doc__ = f"Capture waveform from a channel on {d.name}. Data is saved to a CSV file; returns stats and path."
        return capture_waveform

    def make_set_timebase(d):
        def set_timebase(seconds_per_div: float) -> str:
            try:
                d.set_timebase(seconds_per_div)
                return f"Timebase set to {seconds_per_div} s/div"
            except Exception as e:
                return f"Error: {e}"
        set_timebase.__doc__ = f"Set timebase on {d.name}. Args: seconds_per_div: Time per division in seconds (e.g. 0.001 for 1 ms/div)."
        return set_timebase

    def make_measure(d):
        def measure(parameter: str, channel: int) -> str:
            try:
                value = d.measure(parameter, channel)
                return f"{parameter} on ch{channel}: {value}"
            except Exception as e:
                return f"Error: {e}"
        measure.__doc__ = f"Take an automated measurement on {d.name}. Args: parameter: e.g. frequency, amplitude, mean, rms. channel: Channel number."
        return measure

    def make_screenshot(d):
        def screenshot() -> str:
            try:
                data = d.screenshot()
                path = write_screenshot(d.slug, data)
                if _state.llm:
                    try:
                        description = _state.llm.describe_screenshot(data)
                        return f"{description}\nScreenshot saved to {path}"
                    except Exception as e:
                        _log.warning(f"LLM screenshot description failed: {e}")
                return f"Screenshot saved to {path}"
            except NotImplementedError:
                return f"{d.name} does not support screenshots"
            except Exception as e:
                return f"Error: {e}"
        screenshot.__doc__ = f"Capture a screenshot from {d.name}. Image is saved to a PNG file; returns path and optional description."
        return screenshot

    mcp.add_tool(with_setup_check(make_capture_waveform(driver)), name=f"{slug}_capture_waveform")
    mcp.add_tool(with_setup_check(make_set_timebase(driver)), name=f"{slug}_set_timebase")
    mcp.add_tool(with_setup_check(make_measure(driver)), name=f"{slug}_measure")
    mcp.add_tool(with_setup_check(make_screenshot(driver)), name=f"{slug}_screenshot")


def _register_simulator(driver: SimulatorBase) -> None:
    slug = getattr(driver, "instance_name", driver.slug)

    def make_simulate(d):
        def simulate(node: str, analysis: str = "tran") -> str:
            try:
                data = d.simulate(node, analysis)
                path = write_waveform(data)
                n = len(data.voltage_data)
                if not n:
                    return f"Simulation produced no samples for node '{node}'."
                v_min, v_max = min(data.voltage_data), max(data.voltage_data)
                v_mean = sum(data.voltage_data) / n
                return (
                    f"Simulated node '{node}': {v_max - v_min:.4g} Vpp, mean {v_mean:.4g} V, "
                    f"{n} pts. Saved to {path}"
                )
            except Exception as e:
                return f"Error: {e}"
        simulate.__doc__ = (
            f"Simulate a circuit node with {d.name} on the project's SPICE netlist. "
            "Args: node: net/node name (e.g. 'VOUT'); analysis: 'tran' (default). "
            "Saves the waveform to CSV and returns stats + path."
        )
        return simulate

    def make_list_nodes(d):
        def list_nodes() -> str:
            try:
                nodes = d.list_nodes()
                return ", ".join(nodes) if nodes else "No nodes found."
            except Exception as e:
                return f"Error: {e}"
        list_nodes.__doc__ = f"List the simulatable nodes in {d.name}'s current netlist."
        return list_nodes

    mcp.add_tool(with_setup_check(make_simulate(driver)), name=f"{slug}_simulate")
    mcp.add_tool(with_setup_check(make_list_nodes(driver)), name=f"{slug}_list_nodes")


_REGISTRARS = {
    FunctionGeneratorBase: _register_function_generator,
    OscilloscopeBase: _register_oscilloscope,
    SimulatorBase: _register_simulator,
}


# ── Driver loading ────────────────────────────────────────────────────────────

def _build_config_from_toml(inst_data: dict, cls) -> dict:
    param_map = getattr(cls, "PARAM_MAP", {})
    config: dict[str, str] = {}
    for k, v in inst_data.items():
        if k in ("package", "driver", "type", "limits", "channels"):
            continue
        config[param_map.get(k, k.upper())] = str(v)
    return config


def _load_drivers(
    teasel_config: dict | None = None,
    setup_config: dict | None = None,
) -> list[InstrumentBase]:
    eps = {ep.name: ep for ep in entry_points(group="teasel.instruments")}
    drivers: list[InstrumentBase] = []

    if teasel_config is not None:
        for inst_name, inst_data in teasel_config.get("instruments", {}).items():
            driver_slug = inst_data.get("driver", inst_name)
            ep = eps.get(driver_slug)
            if ep is None:
                print(f"No driver registered for '{driver_slug}' — skipping '{inst_name}'")
                continue
            cls = ep.load()
            config = _build_config_from_toml(inst_data, cls)
            setup_data = (setup_config or {}).get("instruments", {}).get(inst_name, {})
            for ch_name, ch_cfg in setup_data.get("channels", {}).items():
                ch_num = ch_name.lstrip("Cc")
                probe = ch_cfg.get("probe", "none")
                ratio = probe.rstrip("xX") if probe.lower() != "none" else "none"
                config[f"LECROY_PROBE_C{ch_num}"] = ratio
            try:
                driver = cls(config)
                driver.instance_name = inst_name
                driver._limits = _parse_limits(setup_data.get("limits", {}))
                drivers.append(driver)
                print(f"Loaded driver: {driver.name} as '{inst_name}'")
            except Exception as e:
                print(f"Failed to load driver '{inst_name}': {e} (see {_log_file})")
                _log.exception(f"Failed to load driver '{inst_name}'")
    else:
        for ep in eps.values():
            cls = ep.load()
            try:
                driver = cls(dict(os.environ))
                driver.instance_name = driver.slug
                driver._limits = {}
                drivers.append(driver)
                print(f"Loaded driver: {driver.name} ({driver.slug})")
            except Exception as e:
                print(f"Failed to load driver {ep.name}: {e} (see {_log_file})")
                _log.exception(f"Failed to load driver '{ep.name}'")

    return drivers


def _register_driver(driver: InstrumentBase) -> None:
    registered = False
    for base_cls, registrar in _REGISTRARS.items():
        if isinstance(driver, base_cls):
            registrar(driver)
            registered = True

    if not registered:
        print(f"Warning: {driver.slug} has no standard base class — tool consistency not guaranteed")

    def status() -> str:
        try:
            return driver.status()
        except Exception as e:
            return f"Error: {e}"
    status.__doc__ = f"Return connection status and basic info for {driver.name}."

    inst_name = getattr(driver, "instance_name", driver.slug)
    mcp.add_tool(with_setup_check(status), name=f"{inst_name}_status")

    for tool_fn in driver.get_extra_tools():
        mcp.add_tool(with_setup_check(tool_fn))


# ── operator-facing tools ─────────────────────────────────────────────────────

def show_probe_point(net: str) -> str:
    """Tell the human operator where to physically probe. Highlights the named net
    on the web UI's board view and probe table so the operator can move a probe
    there. The operator is a peer, not a remote hand: they may probe a different
    point and tell you (you'll see it as a setup change — call get_setup).

    Args:
        net: the net / node name to probe (e.g. 'output', 'VOUT', 'Net-_C1-Pad1_').
    """
    _push_ui_event({"type": "probe_point", "net": net})
    return (f"Asked the operator to probe net '{net}'. The highlight is best-effort "
            "(a no-op if no web UI is connected). They may probe elsewhere and tell you.")


def _journal_path() -> Path | None:
    """The project's journal Markdown file ([journal].file, default journal.md), or
    None when no project dir is configured (--setup not set)."""
    if _state.setup_path is None:
        return None
    name = _state.setup_cache.get("journal", {}).get("file") or "journal.md"
    return _state.setup_path.parent / name


def append_journal(markdown: str) -> str:
    """Append a section to the project's journal / datasheet document (Markdown). This
    is the safe default — it never overwrites the operator's existing prose. The text
    appears live in the web UI's Journal tab. To include a plot you just made, embed it
    as a Markdown image using a path relative to the journal file so the document stays
    portable, e.g. ![bode](teasel-output/plot_20260614_120000_000000.svg).

    Args:
        markdown: Markdown to append — typically a '## Heading' with prose and image embeds.
    """
    path = _journal_path()
    if path is None:
        return "No project directory configured (--setup not set) — nowhere to write the journal."
    try:
        existing = path.read_text() if path.exists() else ""
        sep = "\n\n" if existing.strip() else ""
        path.write_text(existing + sep + markdown.rstrip() + "\n")
        _push_ui_event({"type": "journal_changed", "path": path.name})
        return f"Appended to {path.name}. It updated live in the Journal tab."
    except Exception as e:
        return f"Error writing journal: {e}"


def write_journal(markdown: str) -> str:
    """Replace the ENTIRE project journal / datasheet document with new Markdown. This
    OVERWRITES everything currently there, including the operator's edits — prefer
    append_journal unless you intend to rewrite the whole document.

    Args:
        markdown: the full Markdown document.
    """
    path = _journal_path()
    if path is None:
        return "No project directory configured (--setup not set) — nowhere to write the journal."
    try:
        path.write_text(markdown.rstrip() + "\n")
        _push_ui_event({"type": "journal_changed", "path": path.name})
        return f"Rewrote {path.name}. It updated live in the Journal tab."
    except Exception as e:
        return f"Error writing journal: {e}"


# ── get_setup tool ────────────────────────────────────────────────────────────

def _get_setup() -> str:
    """Return the current experiment setup (probes, limits, channel labels) and clear
    the pending-change flag. Call this whenever you see a setup-changed warning."""
    if _state.setup_path is None or not _state.setup_path.exists():
        return "No setup.toml configured."
    _check_setup()
    content = _state.setup_path.read_text()
    diff_line = f"\nChanges since last check:\n{_state.setup_diff}" if _state.setup_diff else ""
    # Operator notes ([ui].feedback) are the human's free-text half of the loop —
    # surface them up top rather than buried in the TOML. Kept (rolling/capped), not
    # cleared: a peer's note shouldn't vanish just because a tool happened to run.
    notes = _state.setup_cache.get("ui", {}).get("feedback", [])
    notes_block = ""
    if notes:
        recent = "\n".join(f"  - {n}" for n in notes[-10:])
        notes_block = f"Operator notes (newest last):\n{recent}\n\n"
    _state.setup_changed = False
    _state.setup_diff = ""
    return f"{notes_block}Current setup.toml:\n\n{content}{diff_line}"


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import argparse
    import sys
    from . import get_version
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--version", action="store_true", help="Print version and exit.")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--setup", type=Path, default=None)
    parser.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http"])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7891)
    parser.add_argument("--llm-url", default=None,
                        help="Ollama/llama-server base URL for data preprocessing (e.g. http://localhost:11434)")
    parser.add_argument("--llm-model", default=None, help="Model name for --llm-url")
    parser.add_argument("--cf-account", default=os.environ.get("CF_ACCOUNT_ID"),
                        help="Cloudflare account ID for LLM preprocessing (or CF_ACCOUNT_ID env)")
    parser.add_argument("--cf-token", default=os.environ.get("CF_API_TOKEN"),
                        help="Cloudflare API token (or CF_API_TOKEN env)")
    parser.add_argument("--ui-url", default=os.environ.get("TEASEL_UI_URL", "http://localhost:7890"),
                        help="Web UI base URL for live cues like show_probe_point (or TEASEL_UI_URL env). "
                             "Set this if 'teasel web' runs on a non-default port; pushes are best-effort.")
    args, _ = parser.parse_known_args()

    if args.version:
        print(get_version())
        return

    # Banner to stderr — stdout is the JSON-RPC channel in stdio transport.
    print(f"teasel-server {get_version()}", file=sys.stderr)

    teasel_config = None
    if args.config and args.config.exists():
        teasel_config = tomllib.loads(args.config.read_text())
        print(f"Using config: {args.config}")

    setup_config = None
    if args.setup and args.setup.exists():
        setup_config = tomllib.loads(args.setup.read_text())
        print(f"Using setup: {args.setup}")

    _state.drivers = _load_drivers(teasel_config, setup_config)

    # Always offer the SPICE simulator (a virtual instrument), pointed at the project
    # dir so it can resolve project.toml [netlist].path live. Activated by setting a
    # netlist under Settings → Simulation; simulate() reports that until then.
    if not any(getattr(d, "slug", "") == "spice" for d in _state.drivers):
        try:
            from .instruments.spice.driver import SpiceSimulator
            project_dir = str(args.setup.parent) if args.setup else "."
            sim = SpiceSimulator({"PROJECT_DIR": project_dir})
            sim.instance_name = "spice"
            sim._limits = {}
            _state.drivers.append(sim)
            print(f"Loaded driver: {sim.name} as 'spice'")
        except Exception as e:
            print(f"Failed to load SPICE simulator: {e} (see {_log_file})")
            _log.exception("Failed to load SPICE simulator")

    if not _state.drivers:
        print("No instrument drivers found. Install driver packages and try again.")
    for driver in _state.drivers:
        _register_driver(driver)

    if args.setup and args.setup.exists():
        _state.setup_path = args.setup
        _state.setup_cache = setup_config or {}
        try:
            _state.setup_mtime = args.setup.stat().st_mtime
        except OSError:
            pass

    if args.cf_account and args.cf_token:
        cf_model = args.llm_model or CF_DEFAULT_MODEL
        base_url = CF_BASE_URL.format(account_id=args.cf_account)
        _state.llm = LLMClient(base_url, cf_model, {"Authorization": f"Bearer {args.cf_token}"})
        print(f"LLM preprocessing: Cloudflare Workers AI — {cf_model}")
    elif args.llm_url and args.llm_model:
        _state.llm = LLMClient(f"{args.llm_url}/v1", args.llm_model)
        print(f"LLM preprocessing: {args.llm_url} — {args.llm_model}")

    _state.ui_url = args.ui_url

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.add_tool(_get_setup, name="get_setup")
    mcp.add_tool(with_setup_check(show_probe_point), name="show_probe_point")
    mcp.add_tool(with_setup_check(append_journal), name="append_journal")
    mcp.add_tool(with_setup_check(write_journal), name="write_journal")

    # House-styled plotting (cross-cutting: any trace, any source).
    from .plotting import register_plot_tools

    def _project_style():
        """The project's chosen plot style and the dir relative style paths
        resolve against (project.toml [plot].style, beside project.toml)."""
        spec = (_state.setup_cache.get("plot", {}) or {}).get("style", "")
        pdir = _state.setup_path.parent if _state.setup_path else None
        return spec, pdir

    register_plot_tools(mcp, lambda: _state.drivers, _project_style)

    if args.transport == "streamable-http":
        print(f"Listening on {args.host}:{args.port}")
    try:
        mcp.run(transport=args.transport)
    except KeyboardInterrupt:
        # Ctrl-C: anyio re-raises KeyboardInterrupt after the loop is cancelled.
        # Exit quietly instead of dumping a traceback.
        print("\nShutting down.")


if __name__ == "__main__":
    main()
