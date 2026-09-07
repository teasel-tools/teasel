"""Rohde & Schwarz / Hameg HMO Compact (HMO724) oscilloscope driver.

The HMO724 speaks standard R&S SCPI over its HO720 USB/RS-232 interface, which
appears on Linux as an FTDI virtual COM port (e.g. /dev/ttyUSB0). Commands are
newline-terminated; waveforms are read as ASCII CSV. Verified live against
firmware 04.527 (IDN: HAMEG,HMO724,...).
"""

import io
import json
import threading
import time

import serial

from teasel_server.base import OscilloscopeBase, WaveformData

# R&S returns this reserved IEEE value when a measurement is not available
# (e.g. FREQuency on a non-periodic trace). Treat anything this large as undefined.
_NAN = 9.9e37


class HMOError(Exception):
    pass


class HMOScope:
    """Thin SCPI-over-serial transport for the HMO Compact series."""

    def __init__(self):
        self._ser: "serial.Serial | None" = None
        self._idn = ""

    @property
    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def connect(self, port: str, baud: int = 115200, timeout: float = 5.0) -> str:
        self._ser = serial.Serial(port, baud, timeout=timeout)
        # The FTDI bridge can drop the first command on a cold open; reset buffers
        # and read IDN with one retry before giving up.
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()
        idn = self.query("*IDN?")
        if not idn:
            idn = self.query("*IDN?")
        if not idn:
            raise HMOError(f"No response to *IDN? on {port} @ {baud} baud")
        if "HMO" not in idn.upper() and "HAMEG" not in idn.upper():
            raise HMOError(f"Unexpected instrument on {port}: {idn!r}")
        self._idn = idn
        return idn

    def disconnect(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None

    def _require(self) -> "serial.Serial":
        if self._ser is None or not self._ser.is_open:
            raise HMOError("Not connected")
        return self._ser

    def write(self, cmd: str) -> None:
        ser = self._require()
        ser.write((cmd + "\n").encode())

    def query(self, cmd: str) -> str:
        ser = self._require()
        ser.reset_input_buffer()
        ser.write((cmd + "\n").encode())
        return ser.readline().decode(errors="replace").strip()

    def query_block(self, cmd: str) -> bytes:
        """Query a command that returns an IEEE 488.2 definite-length block:
        #<ndigits><length><bytes>. Used for screenshots (HCOPy:DATA?)."""
        ser = self._require()
        ser.reset_input_buffer()
        ser.write((cmd + "\n").encode())
        # Skip anything up to the leading '#'.
        while True:
            b = ser.read(1)
            if not b:
                raise HMOError(f"Timed out waiting for block header from {cmd!r}")
            if b == b"#":
                break
        ndigits = ser.read(1)
        if not ndigits.isdigit():
            raise HMOError(f"Malformed block header (ndigits={ndigits!r})")
        length = int(ser.read(int(ndigits)).decode())
        data = bytearray()
        while len(data) < length:
            chunk = ser.read(length - len(data))
            if not chunk:
                raise HMOError(f"Block truncated: got {len(data)} of {length} bytes")
            data.extend(chunk)
        ser.readline()  # consume trailing terminator
        return bytes(data)


# Map the generic measurement names the MCP exposes (frequency, amplitude, mean,
# rms, …) to HMO MEASurement:MAIN tokens. Passing a raw HMO token also works.
_MEAS_MAP = {
    "frequency": "FREQ", "freq": "FREQ",
    "period": "PER",
    "amplitude": "AMPL", "ampl": "AMPL",
    "peak": "PEAK", "pkpk": "PEAK", "peak-to-peak": "PEAK", "vpp": "PEAK",
    "max": "UPE", "maximum": "UPE", "upper": "UPE",
    "min": "LPE", "minimum": "LPE", "lower": "LPE",
    "mean": "MEAN", "dc": "MEAN",
    "rms": "RMS",
    "crms": "CRMS", "cycle-rms": "CRMS",
    "stddev": "STDD", "std": "STDD",
    "risetime": "RTIM", "rise": "RTIM",
    "falltime": "FTIM", "fall": "FTIM",
    "poswidth": "POS", "negwidth": "NEG",
    "dutycycle": "DCYC", "duty": "DCYC",
}


class HMO724Driver(OscilloscopeBase):
    PARAM_MAP = {"port": "HMO724_PORT", "baud": "HMO724_BAUD"}

    @property
    def slug(self) -> str:
        return "hmo724"

    @property
    def name(self) -> str:
        return "Hameg HMO724"

    def __init__(self, config: dict):
        self._scope = HMOScope()
        self._lock = threading.Lock()

        port = config.get("HMO724_PORT")
        if not port:
            raise ValueError("HMO724_PORT is required (e.g. /dev/ttyUSB0)")
        self._port = port
        self._baud = int(config.get("HMO724_BAUD", 115200))
        # Connect lazily on first use (see _ensure_connected), not here: this lets
        # the driver load even when the scope is powered off or unplugged at
        # startup. Plugging it in afterwards then works on the next tool call —
        # no server restart needed.

    def _ensure_connected(self) -> None:
        """Open the serial connection if it isn't already, raising HMOError with a
        clear, operator-facing message if the scope can't be reached. Callers must
        hold ``self._lock``. A missing instrument thus surfaces as a tool error
        ("Error: Can't reach the HMO724 …") instead of a failed driver load."""
        if self._scope.is_connected:
            return
        try:
            self._scope.connect(self._port, self._baud)
        except Exception as e:
            raise HMOError(
                f"Can't reach the HMO724 at {self._port}: {e}. "
                "Is it powered on and the USB cable connected?"
            ) from e

    def disconnect(self) -> None:
        self._scope.disconnect()

    # =========================================================================
    # InstrumentBase
    # =========================================================================

    def status(self) -> str:
        with self._lock:
            try:
                self._ensure_connected()
            except HMOError as e:
                return f"{self.name} not connected — {e}"
            tdiv = self._scope.query("TIM:SCAL?")
            srate = self._scope.query("ACQ:SRAT?")
            states = [self._scope.query(f"CHAN{c}:STAT?") for c in (1, 2, 3, 4)]
        on = ", ".join(f"CH{c}" for c, st in enumerate(states, 1) if st.strip() in ("1", "ON"))
        return (
            f"{self.name} connected\n"
            f"  idn:        {self._scope._idn}\n"
            f"  timebase:   {tdiv} s/div\n"
            f"  samplerate: {srate} Sa/s\n"
            f"  channels on: {on or 'none'}"
        )

    # =========================================================================
    # OscilloscopeBase
    # =========================================================================

    def capture_waveform(self, channel: int) -> WaveformData:
        with self._lock:
            self._ensure_connected()
            scope = self._scope
            scope.write(f"CHAN{channel}:STAT ON")
            scope.write("FORM ASC")
            header = scope.query(f"CHAN{channel}:DATA:HEAD?")
            try:
                xstart, xstop, n_str, _vps = header.split(",")
                xstart, xstop, n = float(xstart), float(xstop), int(n_str)
            except ValueError:
                raise HMOError(f"Unexpected DATA:HEADer response: {header!r}")
            if n <= 0:
                raise HMOError(f"CH{channel} reports no samples — is the channel on and triggered?")
            raw = scope._require()
            raw.reset_input_buffer()
            raw.write(f"CHAN{channel}:DATA?\n".encode())
            line = raw.readline().decode(errors="replace").strip()

        parts = line.split(",")
        try:
            voltages = [float(p) for p in parts]
        except ValueError:
            raise HMOError(f"Could not parse waveform data (got {len(parts)} fields)")
        dt = (xstop - xstart) / (n - 1) if n > 1 else 0.0
        time_data = [round(xstart + i * dt, 12) for i in range(len(voltages))]
        return WaveformData(
            channel=channel,
            time_data=time_data,
            voltage_data=voltages,
            sample_rate=1.0 / dt if dt else 0.0,
            metadata={"model": "HMO724"},
        )

    def set_timebase(self, seconds_per_div: float) -> None:
        with self._lock:
            self._ensure_connected()
            self._scope.write(f"TIM:SCAL {seconds_per_div:.6E}")

    def measure(self, parameter: str, channel: int) -> float:
        token = _MEAS_MAP.get(parameter.strip().lower(), parameter.strip().upper())
        with self._lock:
            self._ensure_connected()
            scope = self._scope
            scope.write(f"MEAS1:SOUR CH{channel}")
            scope.write(f"MEAS1:MAIN {token}")
            raw = scope.query("MEAS1:RES?")
        try:
            value = float(raw)
        except ValueError:
            raise HMOError(f"C{channel} {parameter}: unexpected response {raw!r}")
        if abs(value) >= _NAN:
            raise HMOError(
                f"C{channel} {parameter} ({token}): undefined — channel may be off, "
                "untriggered, or the signal isn't periodic for this measurement."
            )
        return value

    def screenshot(self) -> bytes:
        from PIL import Image
        with self._lock:
            self._ensure_connected()
            self._scope.write("HCOP:LANG PNG")
            raw = self._scope.query_block("HCOP:DATA?")
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    # =========================================================================
    # Extra tools — HMO-specific features
    # =========================================================================

    def get_extra_tools(self) -> list:
        scope = self._scope
        lock = self._lock
        driver = self

        def _run(fn):
            with lock:
                try:
                    driver._ensure_connected()
                    result = fn()
                    return str(result) if result is not None else "OK"
                except Exception as e:
                    return f"ERROR: {e}"

        def hmo724_configure_channel(
            channel: int,
            scale: float = None,
            position: float = None,
            coupling: str = None,
            state: bool = None,
        ) -> str:
            """Configure a channel. Args: channel: 1-4. scale: V/div. position: vertical
            position in divisions. coupling: DCL/ACL/GND. state: on/off."""
            def _apply():
                applied = []
                if state    is not None: scope.write(f"CHAN{channel}:STAT {'ON' if state else 'OFF'}"); applied.append(f"state={'ON' if state else 'OFF'}")
                if scale    is not None: scope.write(f"CHAN{channel}:SCAL {scale:.6E}");                 applied.append(f"scale={scale}")
                if position is not None: scope.write(f"CHAN{channel}:POS {position:.4f}");                applied.append(f"position={position}")
                if coupling is not None: scope.write(f"CHAN{channel}:COUP {coupling.upper()}");           applied.append(f"coupling={coupling.upper()}")
                return f"CH{channel}: " + ", ".join(applied) if applied else "Nothing changed."
            return _run(_apply)

        def hmo724_configure_trigger(
            source: str = None,
            slope: str = None,
            level: float = None,
            mode: str = None,
        ) -> str:
            """Configure the edge trigger. Args: source: CH1-CH4/EXT. slope: POS/NEG/EITH.
            level: volts (applied to the source channel). mode: AUTO/NORM."""
            def _apply():
                applied = []
                if source is not None: scope.write(f"TRIG:A:SOUR {source.upper()}");  applied.append(f"source={source.upper()}")
                if slope  is not None: scope.write(f"TRIG:A:EDGE:SLOP {slope.upper()}"); applied.append(f"slope={slope.upper()}")
                if mode   is not None: scope.write(f"TRIG:A:MODE {mode.upper()}");      applied.append(f"mode={mode.upper()}")
                if level  is not None:
                    src = (source or scope.query("TRIG:A:SOUR?")).upper()
                    ch = "".join(ch for ch in src if ch.isdigit()) or "1"
                    scope.write(f"TRIG:A:LEV{ch} {level:.6E}")
                    applied.append(f"level={level}V")
                return "Trigger: " + ", ".join(applied) if applied else "Nothing changed."
            return _run(_apply)

        def hmo724_run() -> str:
            """Start continuous acquisition (RUN)."""
            return _run(lambda: scope.write("RUN"))

        def hmo724_stop() -> str:
            """Stop acquisition (STOP)."""
            return _run(lambda: scope.write("STOP"))

        def hmo724_single() -> str:
            """Arm a single acquisition and wait for it to complete."""
            def _fn():
                scope.write("SING")
                scope.query("*OPC?")
                return "Acquired."
            return _run(_fn)

        def hmo724_autoset() -> str:
            """Run the scope's AUTOSET (auto-scale all active channels)."""
            def _fn():
                scope.write("AUT")
                time.sleep(2.0)
                return "Autoset done."
            return _run(_fn)

        def hmo724_measure_all(channel: int) -> str:
            """Common measurements for a channel (PEAK/Vpp, UPE/max, LPE/min, MEAN, RMS,
            FREQ, PER). Args: channel: 1-4."""
            def _fn():
                scope.write(f"MEAS1:SOUR CH{channel}")
                lines = [f"CH{channel} measurements:"]
                for label, tok in [("Vpp", "PEAK"), ("max", "UPE"), ("min", "LPE"),
                                   ("mean", "MEAN"), ("rms", "RMS"),
                                   ("freq", "FREQ"), ("period", "PER")]:
                    scope.write(f"MEAS1:MAIN {tok}")
                    raw = scope.query("MEAS1:RES?")
                    try:
                        v = float(raw)
                        val = "UNDEF" if abs(v) >= _NAN else f"{v:.6g}"
                    except ValueError:
                        val = raw
                    lines.append(f"  {label:7s}: {val}")
                return "\n".join(lines)
            return _run(_fn)

        def hmo724_query(command: str) -> str:
            """Raw SCPI query escape hatch. Args: command: SCPI query string (e.g. 'TIM:SCAL?')."""
            return _run(lambda: scope.query(command))

        def hmo724_write(command: str) -> str:
            """Raw SCPI write escape hatch. Args: command: SCPI command string."""
            return _run(lambda: scope.write(command))

        return [
            hmo724_configure_channel,
            hmo724_configure_trigger,
            hmo724_run,
            hmo724_stop,
            hmo724_single,
            hmo724_autoset,
            hmo724_measure_all,
            hmo724_query,
            hmo724_write,
        ]
