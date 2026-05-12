"""Unified MCP server — discovers instrument drivers via entry points and registers tools."""

import functools
import os
import tomllib
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Callable

from mcp.server.fastmcp import FastMCP

from .base import FunctionGeneratorBase, InstrumentBase, OscilloscopeBase
from .output import write_screenshot, write_waveform

mcp = FastMCP("teasel")


# ── Server state ──────────────────────────────────────────────────────────────

@dataclass
class _ServerState:
    setup_path: Path | None = None
    setup_mtime: float = 0.0
    setup_cache: dict = field(default_factory=dict)
    setup_changed: bool = False
    setup_diff: str = ""
    drivers: list[InstrumentBase] = field(default_factory=list)


_state = _ServerState()


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


def _apply_setup(setup_config: dict) -> None:
    """Push updated limits from setup_config into all running drivers."""
    for driver in _state.drivers:
        inst_name = getattr(driver, "instance_name", driver.slug)
        data = setup_config.get("instruments", {}).get(inst_name, {})
        driver._limits = {k: float(v) for k, v in data.get("limits", {}).items()}


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

    def make_set_frequency(d):
        def set_frequency(freq_hz: float) -> str:
            limits = getattr(d, "_limits", {})
            if "frequency_max" in limits and freq_hz > limits["frequency_max"]:
                return f"Limit exceeded: requested {freq_hz} Hz but safety limit is {limits['frequency_max']} Hz"
            try:
                d.set_frequency(freq_hz)
                return f"Frequency set to {freq_hz} Hz"
            except Exception as e:
                return f"Error: {e}"
        limit_note = f" Safety limit: {d._limits['frequency_max']} Hz." if getattr(d, "_limits", {}).get("frequency_max") else ""
        set_frequency.__doc__ = f"Set output frequency on {d.name}. Args: freq_hz: Frequency in Hz.{limit_note}"
        return set_frequency

    def make_set_amplitude(d):
        def set_amplitude(vpp: float, dc: float = 0.0) -> str:
            limits = getattr(d, "_limits", {})
            if "amplitude_max" in limits and vpp > limits["amplitude_max"]:
                return f"Limit exceeded: requested {vpp} Vpp but safety limit is {limits['amplitude_max']} Vpp"
            try:
                d.set_amplitude(vpp, dc)
                return f"Amplitude set to {vpp} Vpp, DC offset {dc} V"
            except Exception as e:
                return f"Error: {e}"
        limit_note = f" Safety limit: {d._limits['amplitude_max']} Vpp." if getattr(d, "_limits", {}).get("amplitude_max") else ""
        set_amplitude.__doc__ = f"Set output amplitude on {d.name}. Args: vpp: Peak-to-peak voltage. dc: DC offset in volts.{limit_note}"
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
            try:
                d.configure(freq_hz, vpp, dc, waveform)
                return f"Configured: {freq_hz} Hz, {vpp} Vpp, {dc} V DC, {waveform}"
            except Exception as e:
                return f"Error: {e}"
        configure.__doc__ = f"Set frequency, amplitude, DC offset, and waveform in one call on {d.name}."
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
                return f"Waveform saved to {path}"
            except Exception as e:
                return f"Error: {e}"
        capture_waveform.__doc__ = f"Capture waveform from a channel on {d.name}. Data is saved to a CSV file; returns the path."
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
                return f"Screenshot saved to {path}"
            except NotImplementedError:
                return f"{d.name} does not support screenshots"
            except Exception as e:
                return f"Error: {e}"
        screenshot.__doc__ = f"Capture a screenshot from {d.name}. Image is saved to a PNG file; returns the path."
        return screenshot

    mcp.add_tool(with_setup_check(make_capture_waveform(driver)), name=f"{slug}_capture_waveform")
    mcp.add_tool(with_setup_check(make_set_timebase(driver)), name=f"{slug}_set_timebase")
    mcp.add_tool(with_setup_check(make_measure(driver)), name=f"{slug}_measure")
    mcp.add_tool(with_setup_check(make_screenshot(driver)), name=f"{slug}_screenshot")


_REGISTRARS = {
    FunctionGeneratorBase: _register_function_generator,
    OscilloscopeBase: _register_oscilloscope,
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
                driver._limits = {k: float(v) for k, v in setup_data.get("limits", {}).items()}
                drivers.append(driver)
                print(f"Loaded driver: {driver.name} as '{inst_name}'")
            except Exception as e:
                print(f"Failed to load driver '{inst_name}': {e}")
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
                print(f"Failed to load driver {ep.name}: {e}")

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


# ── get_setup tool ────────────────────────────────────────────────────────────

def _get_setup() -> str:
    """Return the current experiment setup (probes, limits, channel labels) and clear
    the pending-change flag. Call this whenever you see a setup-changed warning."""
    if _state.setup_path is None or not _state.setup_path.exists():
        return "No setup.toml configured."
    _check_setup()
    content = _state.setup_path.read_text()
    diff_line = f"\nChanges since last check:\n{_state.setup_diff}" if _state.setup_diff else ""
    _state.setup_changed = False
    _state.setup_diff = ""
    return f"Current setup.toml:\n\n{content}{diff_line}"


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--setup", type=Path, default=None)
    args, _ = parser.parse_known_args()

    teasel_config = None
    if args.config and args.config.exists():
        teasel_config = tomllib.loads(args.config.read_text())
        print(f"Using config: {args.config}")

    setup_config = None
    if args.setup and args.setup.exists():
        setup_config = tomllib.loads(args.setup.read_text())
        print(f"Using setup: {args.setup}")

    _state.drivers = _load_drivers(teasel_config, setup_config)
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

    mcp.add_tool(_get_setup, name="get_setup")
    mcp.run(transport="stdio")
