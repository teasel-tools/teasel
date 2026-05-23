"""Dummy instrument driver for testing without hardware."""

import math
import time

from teasel_server.base import FunctionGeneratorBase, OscilloscopeBase, WaveformData


class DummyFunctionGenerator(FunctionGeneratorBase):
    waveforms = ["sine", "square", "triangle", "sawtooth"]

    @property
    def slug(self) -> str:
        return "dummy-fg"

    @property
    def name(self) -> str:
        return "Dummy Function Generator"

    def __init__(self, config: dict):
        self._freq_hz = 1000.0
        self._vpp = 1.0
        self._dc = 0.0
        self._waveform = "sine"

    def status(self) -> str:
        return (
            f"{self.name} (simulated) — "
            f"{self._freq_hz} Hz, {self._vpp} Vpp, {self._dc} V DC, {self._waveform}"
        )

    def set_frequency(self, freq_hz: float) -> None:
        self._freq_hz = freq_hz

    def set_amplitude(self, vpp: float, dc: float = 0.0) -> None:
        self._vpp = vpp
        self._dc = dc

    def set_waveform(self, waveform: str) -> None:
        if waveform not in self.waveforms:
            raise ValueError(f"Unknown waveform '{waveform}', must be one of: {self.waveforms}")
        self._waveform = waveform


class DummyOscilloscope(OscilloscopeBase):
    @property
    def slug(self) -> str:
        return "dummy-scope"

    @property
    def name(self) -> str:
        return "Dummy Oscilloscope"

    def __init__(self, config: dict):
        self._seconds_per_div = 0.001
        self._num_divs = 10
        self._sample_rate = 1_000_000.0

    def status(self) -> str:
        return f"{self.name} (simulated) — {self._seconds_per_div * 1000} ms/div"

    def set_timebase(self, seconds_per_div: float) -> None:
        self._seconds_per_div = seconds_per_div

    def capture_waveform(self, channel: int) -> WaveformData:
        duration = self._seconds_per_div * self._num_divs
        n = int(self._sample_rate * duration)
        t = [i / self._sample_rate for i in range(n)]
        freq = 1000.0
        v = [math.sin(2 * math.pi * freq * ti) for ti in t]
        return WaveformData(
            channel=channel,
            time_data=t,
            voltage_data=v,
            sample_rate=self._sample_rate,
        )

    def measure(self, parameter: str, channel: int) -> float:
        values = {
            "frequency": 1000.0,
            "amplitude": 1.0,
            "mean": 0.0,
            "rms": 0.707,
        }
        if parameter not in values:
            raise ValueError(f"Unknown parameter '{parameter}', must be one of: {list(values)}")
        return values[parameter]
