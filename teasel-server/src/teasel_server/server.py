"""Unified MCP server — discovers instrument drivers via entry points and registers tools."""

import os
from importlib.metadata import entry_points

from mcp.server.fastmcp import FastMCP

from .base import FunctionGeneratorBase, OscilloscopeBase, InstrumentBase
from .output import write_waveform, write_screenshot

mcp = FastMCP("teasel")


def _register_function_generator(driver: FunctionGeneratorBase) -> None:
    slug = driver.slug

    def make_set_frequency(d):
        def set_frequency(freq_hz: float) -> str:
            try:
                d.set_frequency(freq_hz)
                return f"Frequency set to {freq_hz} Hz"
            except Exception as e:
                return f"Error: {e}"
        set_frequency.__doc__ = f"Set output frequency on {d.name}. Args: freq_hz: Frequency in Hz."
        return set_frequency

    def make_set_amplitude(d):
        def set_amplitude(vpp: float, dc: float = 0.0) -> str:
            try:
                d.set_amplitude(vpp, dc)
                return f"Amplitude set to {vpp} Vpp, DC offset {dc} V"
            except Exception as e:
                return f"Error: {e}"
        set_amplitude.__doc__ = f"Set output amplitude on {d.name}. Args: vpp: Peak-to-peak voltage. dc: DC offset in volts."
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

    mcp.add_tool(make_set_frequency(driver), name=f"{slug}_set_frequency")
    mcp.add_tool(make_set_amplitude(driver), name=f"{slug}_set_amplitude")
    mcp.add_tool(make_set_waveform(driver), name=f"{slug}_set_waveform")
    mcp.add_tool(make_configure(driver), name=f"{slug}_configure")


def _register_oscilloscope(driver: OscilloscopeBase) -> None:
    slug = driver.slug

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

    mcp.add_tool(make_capture_waveform(driver), name=f"{slug}_capture_waveform")
    mcp.add_tool(make_set_timebase(driver), name=f"{slug}_set_timebase")
    mcp.add_tool(make_measure(driver), name=f"{slug}_measure")
    mcp.add_tool(make_screenshot(driver), name=f"{slug}_screenshot")


_REGISTRARS = {
    FunctionGeneratorBase: _register_function_generator,
    OscilloscopeBase: _register_oscilloscope,
}


def _load_drivers() -> list[InstrumentBase]:
    config = dict(os.environ)
    drivers = []
    for ep in entry_points(group="teasel.instruments"):
        cls = ep.load()
        try:
            driver = cls(config)
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

    mcp.add_tool(status, name=f"{driver.slug}_status")

    for tool_fn in driver.get_extra_tools():
        mcp.add_tool(tool_fn)


def main():
    drivers = _load_drivers()
    if not drivers:
        print("No instrument drivers found. Install driver packages and try again.")
    for driver in drivers:
        _register_driver(driver)
    mcp.run(transport="stdio")
