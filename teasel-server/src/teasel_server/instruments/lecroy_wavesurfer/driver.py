import io
import json
import threading
from datetime import datetime
from pathlib import Path

from teasel_server.base import FunctionGeneratorBase, OscilloscopeBase, WaveformData
from teasel_server.output import output_dir, write_screenshot
from .oscilloscope import LeCroyScope, InstrumentError


def _parse_probe_env(val: str) -> "dict | None":
    if val.strip().lower() == "none":
        return None
    parts = val.split(",")
    return {"ratio": float(parts[0].strip()), "unit": parts[1].strip() if len(parts) > 1 else "V"}


class LecroyWaveSurferDriver(OscilloscopeBase, FunctionGeneratorBase):
    slug = "lecroy-wavesurfer"
    name = "LeCroy WaveSurfer"
    waveforms = ["Sine", "Square", "Triangle", "Pulse", "DC", "Noise", "Arb"]
    PARAM_MAP = {"host": "LECROY_HOST", "resource": "LECROY_RESOURCE"}

    def __init__(self, config: dict):
        self._scope = LeCroyScope()
        self._lock = threading.Lock()

        resource = config.get("LECROY_RESOURCE")
        host = config.get("LECROY_HOST")
        if not resource and not host:
            raise ValueError("LECROY_RESOURCE or LECROY_HOST is required")

        resource_string = resource or f"TCPIP0::{host}::inst0::INSTR"
        self._scope.connect(resource_string)

        # Apply probe config from env
        self._probes: dict = {}
        for ch in (1, 2, 3, 4):
            val = config.get(f"LECROY_PROBE_C{ch}")
            if val is not None:
                self._probes[ch] = _parse_probe_env(val)
        self._apply_probes()

    def _apply_probes(self) -> None:
        for ch, probe in self._probes.items():
            if probe is None:
                continue
            self._scope.set_attenuation(ch, probe["ratio"])

    def _run(self, fn):
        with self._lock:
            try:
                result = fn()
                return str(result) if result is not None else "OK"
            except InstrumentError as e:
                return f"ERROR: {e}"
            except Exception as e:
                return f"ERROR: {e}"

    def _probe_warning(self, channel: int) -> str:
        if self._probes.get(channel) is None and channel in self._probes:
            return f"WARNING: C{channel} is marked as not connected — data may be noise.\n"
        return ""

    # =========================================================================
    # InstrumentBase
    # =========================================================================

    def status(self) -> str:
        if not self._scope.is_connected:
            return f"{self.name} not connected"
        caps = self._scope.get_capabilities()
        return (
            f"{self.name} connected\n"
            f"  model:     {caps['model']}\n"
            f"  family:    {caps['family']}\n"
            f"  channels:  {caps['channels']}\n"
            f"  bandwidth: {caps['bandwidth_mhz']} MHz\n"
            f"  wavesource: {'yes' if caps.get('has_wavesource') else 'no'}"
        )

    # =========================================================================
    # OscilloscopeBase
    # =========================================================================

    def capture_waveform(self, channel: int) -> WaveformData:
        with self._lock:
            if self._scope.is_stopped():
                self._scope.arm_and_wait()
            self._scope.stop()
            data = self._scope.get_waveform(channel, max_points=10000)
        dt = data["sample_interval_s"]
        time_data = [round(i * dt, 12) for i in range(data["num_points"])]
        return WaveformData(
            channel=channel,
            time_data=time_data,
            voltage_data=data["voltages"],
            sample_rate=1.0 / dt if dt else 0,
            metadata={"model": self._scope._model or "unknown"},
        )

    def set_timebase(self, seconds_per_div: float) -> None:
        with self._lock:
            self._scope.set_tdiv(seconds_per_div)

    def measure(self, parameter: str, channel: int) -> float:
        with self._lock:
            if self._scope.is_stopped():
                raise InstrumentError(
                    "Scope is stopped — measurements will be stale. "
                    "Use lecroy_configure_trigger(mode='AUTO') or mode='NORM' for continuous acquisition, "
                    "or lecroy_arm_and_wait() for a single shot."
                )
            raw = self._scope.measure(channel, parameter.upper())
        # PAVA returns e.g. "C1:PAVA PKPK,773E-3 V,OK"
        try:
            value_str = raw.split(",")[1].strip()
        except IndexError:
            raise InstrumentError(f"Unexpected response: {raw!r}")
        if value_str == "UNDEF":
            raise InstrumentError(f"C{channel} {parameter}: UNDEF — channel may be off or untriggered")
        try:
            return float(value_str.split()[0])
        except ValueError:
            raise InstrumentError(f"Could not parse value {value_str!r} from: {raw!r}")

    def screenshot(self) -> bytes:
        from PIL import Image
        with self._lock:
            raw = self._scope.get_screenshot()
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    # =========================================================================
    # FunctionGeneratorBase (WaveSource — model-dependent)
    # =========================================================================

    def _require_wavesource(self) -> None:
        if not self._scope.profile.has_wavesource:
            raise InstrumentError(
                f"{self._scope.profile.family} does not have a WaveSource generator"
            )

    def set_frequency(self, freq_hz: float) -> None:
        with self._lock:
            self._require_wavesource()
            self._scope.wavesource_set_frequency(freq_hz)

    def set_amplitude(self, vpp: float, dc: float = 0.0) -> None:
        with self._lock:
            self._require_wavesource()
            self._scope.wavesource_set_amplitude(vpp)
            if dc != 0.0:
                self._scope.wavesource_set_offset(dc)

    def set_waveform(self, waveform: str) -> None:
        with self._lock:
            self._require_wavesource()
            self._scope.wavesource_set_shape(waveform)

    def configure(self, freq_hz: float, vpp: float, dc: float = 0.0, waveform: str = "Sine") -> None:
        with self._lock:
            self._require_wavesource()
            self._scope.wavesource_set_shape(waveform)
            self._scope.wavesource_set_frequency(freq_hz)
            self._scope.wavesource_set_amplitude(vpp)
            self._scope.wavesource_set_offset(dc)

    # =========================================================================
    # Extra tools — LeCroy-specific features
    # =========================================================================

    def get_extra_tools(self) -> list:
        scope = self._scope
        lock = self._lock
        slug = self.slug

        def _run(fn):
            with lock:
                try:
                    result = fn()
                    return str(result) if result is not None else "OK"
                except InstrumentError as e:
                    return f"ERROR: {e}"
                except Exception as e:
                    return f"ERROR: {e}"

        def lecroy_scan(subnet: str = "") -> str:
            """Scan the network for LeCroy oscilloscopes. Args: subnet: CIDR, e.g. 192.168.1.0/24"""
            try:
                results = LeCroyScope.scan_network(subnet) if subnet else []
                if not results:
                    return "No LeCroy instruments found."
                return "\n".join(f"{r}  —  {idn}" for r, idn in results)
            except Exception as e:
                return f"ERROR: {e}"

        def lecroy_capabilities() -> str:
            """Return full capability profile of the connected LeCroy."""
            return _run(lambda: json.dumps(scope.get_capabilities(), indent=2))

        def lecroy_configure_channel(
            channel: int,
            vdiv: float = None,
            offset: float = None,
            coupling: str = None,
            bwlimit: str = None,
            invert: bool = None,
            trace: bool = None,
        ) -> str:
            """Configure channel settings. Args: channel: 1-4. vdiv: V/div. offset: volts. coupling: D1M/D50/A1M/GND. bwlimit: OFF/20MHZ/200MHZ. invert: bool. trace: bool."""
            def _apply():
                applied = []
                if vdiv     is not None: scope.set_vdiv(channel, vdiv);        applied.append(f"vdiv={vdiv}")
                if offset   is not None: scope.set_offset(channel, offset);    applied.append(f"offset={offset}")
                if coupling is not None: scope.set_coupling(channel, coupling); applied.append(f"coupling={coupling}")
                if bwlimit  is not None: scope.set_bwlimit(channel, bwlimit);  applied.append(f"bwlimit={bwlimit}")
                if invert   is not None: scope.set_invert(channel, invert);    applied.append(f"invert={invert}")
                if trace    is not None: scope.set_trace(channel, trace);      applied.append(f"trace={trace}")
                return f"C{channel}: " + ", ".join(applied) if applied else "Nothing changed."
            return _run(_apply)

        def lecroy_configure_trigger(
            mode: str = None,
            source: str = None,
            slope: str = None,
            level: float = None,
        ) -> str:
            """Configure trigger. Args: mode: AUTO/NORM/SINGLE/STOP. source: C1-C4/EX/LINE. slope: POS/NEG/EITHER. level: volts."""
            import re
            def _apply():
                applied = []
                if mode   is not None: scope.set_trigger_mode(mode);                    applied.append(f"mode={mode}")
                if source is not None: scope.set_trigger_source(source, slope or "POS"); applied.append(f"source={source}")
                if level  is not None:
                    ch_num = 1
                    if source:
                        m = re.match(r"[Cc](\d+)", source)
                        if m:
                            ch_num = int(m.group(1))
                    scope.set_trigger_level(ch_num, level)
                    applied.append(f"level={level}V")
                return "Trigger: " + ", ".join(applied) if applied else "Nothing changed."
            return _run(_apply)

        def lecroy_arm_and_wait(timeout_s: float = 10.0) -> str:
            """Arm the scope and wait for one acquisition to complete. Best used with NORM or SINGLE trigger mode.
            In AUTO mode the scope is already continuously acquiring — calling this will likely time out.
            For a sweep in AUTO mode, just call measure or measure_all directly without arming first.
            Args: timeout_s: max wait time in seconds."""
            def _fn():
                ok = scope.arm_and_wait(timeout_s)
                return "Acquired." if ok else f"Timed out after {timeout_s} s."
            return _run(_fn)

        def lecroy_capture_channels(channels: list[int], max_points: int = 10000) -> str:
            """Capture multiple channels atomically and save to .npz. Args: channels: list of channel numbers. max_points: samples per channel."""
            import numpy as np
            def _fn():
                if scope.is_stopped():
                    scope.arm_and_wait()
                scope.stop()
                waveforms = scope.get_waveforms(channels, max_points)
                dt = waveforms[0]["sample_interval_s"]
                n = waveforms[0]["num_points"]
                time_arr = [round(i * dt, 12) for i in range(n)]
                arrays = {"time_s": time_arr}
                for wf in waveforms:
                    arrays[f"c{wf['channel']}"] = wf["voltages"]
                ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:21]
                ch_tag = "".join(f"C{c}" for c in channels)
                path = output_dir() / f"{ch_tag}_{ts}.npz"
                np.savez(str(path), **arrays)
                return json.dumps({
                    "data_file": str(path),
                    "channels": channels,
                    "num_points": n,
                    "sample_interval_s": dt,
                })
            return _run(_fn)

        def lecroy_set_math(func: int, equation: str) -> str:
            """Set a math function. Args: func: 1-4. equation: e.g. FFT(C1), C1+C2."""
            return _run(lambda: scope.set_math(func, equation))

        def lecroy_measure_all(channel: int) -> str:
            """Get common PAVA measurements for a channel (PKPK, FREQ, MEAN, RMS, AMPL, MAX, MIN, PERIOD).
            Args: channel: 1-4."""
            def _fn():
                if scope.is_stopped():
                    return (
                        "Scope is stopped — measurements will be stale. "
                        "Use lecroy_configure_trigger(mode='AUTO') or mode='NORM' for continuous acquisition, "
                        "or lecroy_arm_and_wait() for a single shot."
                    )
                results = scope.measure_all(channel)
                lines = [f"C{channel} measurements:"]
                for k, v in sorted(results.items()):
                    lines.append(f"  {k:8s}: {f'{v:.6g}' if v is not None else 'UNDEF'}")
                return "\n".join(lines)
            return _run(_fn)

        def lecroy_decode_read(decoder: int = 1) -> str:
            """Read serial decode table and save to .npz. Args: decoder: 1 or 2."""
            import numpy as np
            def _fn():
                result = scope.decode_read(decoder)
                if result["rows"] == 0:
                    return json.dumps({"decoder": decoder, "num_frames": 0})
                arrays: dict = {}
                if result["time_s"]:
                    arrays["time_s"] = [x if x is not None else float("nan") for x in result["time_s"]]
                if result["data"]:
                    arrays["data"] = [x if x is not None else 0 for x in result["data"]]
                ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:21]
                path = output_dir() / f"decode{decoder}_{ts}.npz"
                np.savez(str(path), **{k: np.array(v) for k, v in arrays.items()})
                return json.dumps({"data_file": str(path), "num_frames": result["rows"]})
            return _run(_fn)

        def lecroy_query(command: str) -> str:
            """Raw SCPI query escape hatch. Args: command: SCPI query string."""
            return _run(lambda: scope.query(command))

        def lecroy_write(command: str) -> str:
            """Raw SCPI write escape hatch. Args: command: SCPI command string."""
            return _run(lambda: scope.write(command))

        def lecroy_wavesource_info() -> str:
            """Get current WaveSource generator settings (WaveSurfer 3000Z and similar)."""
            def _fn():
                info = scope.get_wavesource_info()
                lines = ["WaveSource:"]
                for k, v in info.items():
                    lines.append(f"  {k:12s}: {v}")
                return "\n".join(lines)
            return _run(_fn)

        tools = [
            lecroy_scan,
            lecroy_capabilities,
            lecroy_configure_channel,
            lecroy_configure_trigger,
            lecroy_arm_and_wait,
            lecroy_capture_channels,
            lecroy_set_math,
            lecroy_measure_all,
            lecroy_decode_read,
            lecroy_query,
            lecroy_write,
        ]

        if self._scope.profile.has_wavesource:
            tools.append(lecroy_wavesource_info)

        return tools
