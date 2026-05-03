"""PM5190 LF synthesizer interface via AR488 serial-to-GPIB adapter."""

import time
import threading
import serial


class PM5190Error(Exception):
    pass


def _range_i(vpp: float) -> int:
    return 0 if vpp < 0.2 else (1 if vpp < 2.0 else 2)


def _fmt_ac(vpp: float) -> str:
    """Format amplitude using same string-slice algorithm as PM5190 BASIC manual examples."""
    i = _range_i(vpp)
    s = f" {100.0005 + vpp:.4f}"
    return s[4 - i : 8 - i]


def _fmt_dc(dc: float, vpp: float) -> str:
    """Format DC offset; decimal position implied by amplitude range."""
    i = _range_i(vpp)
    sign = "-" if dc < 0 else ""
    s = f" {10.00005 + abs(dc) / 10:.5f}"
    return sign + s[6 - i : 8 - i]


def build_command(freq_hz: float, vpp: float, dc: float, waveform: int) -> str:
    return f"F{freq_hz / 1000:g}A{_fmt_ac(vpp)}D{_fmt_dc(dc, vpp)}W{waveform}"


class PM5190:
    WAVEFORMS = {1: "sine", 2: "square", 3: "triangle", 4: "sine/AM ext", 5: "triangle/AM ext"}

    def __init__(self) -> None:
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()
        self.port = "/dev/ttyUSB0"
        self.baud = 115200
        self.gpib_addr = 4
        self.firmware: str = ""

    @property
    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def connect(self, port: str, baud: int = 115200, gpib_addr: int = 4) -> str:
        """Connect to the AR488 adapter and initialize GPIB controller mode.

        Returns the AR488 firmware version string.
        """
        with self._lock:
            if self._ser and self._ser.is_open:
                self._ser.close()
            self.port = port
            self.baud = baud
            self.gpib_addr = gpib_addr
            try:
                self._ser = serial.Serial(port, baud, timeout=1)
                time.sleep(2.0)  # wait for Arduino DTR reset
                self._ser.write(b"++ver\r\n")
                time.sleep(0.05)
                self.firmware = self._ser.readline().decode(errors="replace").strip()
                for cmd in ("++mode 1", f"++addr {gpib_addr}", "++eos 3"):
                    self._ser.write((cmd + "\r\n").encode())
                    time.sleep(0.05)
                return self.firmware
            except serial.SerialException as e:
                self._ser = None
                raise PM5190Error(str(e)) from e

    def disconnect(self) -> None:
        with self._lock:
            if self._ser:
                self._ser.close()
                self._ser = None
            self.firmware = ""

    def _send(self, cmd: str) -> None:
        if not self.is_connected:
            raise PM5190Error("Not connected")
        self._ser.write((cmd + "\x03\r\n").encode())

    def configure(self, freq_hz: float, vpp: float, dc: float, waveform: int) -> str:
        """Set all parameters in a single GPIB command."""
        with self._lock:
            cmd = build_command(freq_hz, vpp, dc, waveform)
            self._send(cmd)
            return cmd

    def set_frequency(self, freq_hz: float) -> str:
        """Set frequency without changing other parameters."""
        with self._lock:
            cmd = f"F{freq_hz / 1000:g}"
            self._send(cmd)
            return cmd

    def set_amplitude(self, vpp: float, dc: float = 0.0) -> str:
        """Set amplitude and DC offset. Both must be set together as DC format depends on amplitude range."""
        with self._lock:
            cmd = f"A{_fmt_ac(vpp)}D{_fmt_dc(dc, vpp)}"
            self._send(cmd)
            return cmd

    def set_waveform(self, waveform: int) -> str:
        """Set waveform type (1=sine, 2=square, 3=triangle, 4=sine/AM ext, 5=triangle/AM ext)."""
        with self._lock:
            cmd = f"W{waveform}"
            self._send(cmd)
            return cmd
