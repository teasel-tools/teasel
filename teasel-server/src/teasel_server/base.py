from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class WaveformData:
    channel: int
    time_data: list[float]
    voltage_data: list[float]
    sample_rate: float
    units: str = "V"
    metadata: dict = field(default_factory=dict)


class InstrumentBase(ABC):
    @property
    @abstractmethod
    def slug(self) -> str: ...

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def __init__(self, config: dict): ...

    def connect(self) -> None:
        """Open the connection to the instrument. Override if the driver needs explicit setup."""

    def disconnect(self) -> None:
        """Close the connection to the instrument. Override if the driver needs explicit teardown."""

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

    def status(self) -> str:
        return f"{self.name} connected"

    def get_extra_tools(self) -> list:
        """Override to register instrument-specific tools beyond the base set."""
        return []


class FunctionGeneratorBase(InstrumentBase):
    waveforms: list[str] = ["sine", "square", "triangle"]

    @abstractmethod
    def set_frequency(self, freq_hz: float) -> None: ...

    @abstractmethod
    def set_amplitude(self, vpp: float, dc: float = 0.0) -> None: ...

    @abstractmethod
    def set_waveform(self, waveform: str) -> None: ...

    def configure(self, freq_hz: float, vpp: float, dc: float = 0.0, waveform: str = "sine") -> None:
        """Set all parameters at once. Default implementation calls individual setters."""
        self.set_frequency(freq_hz)
        self.set_amplitude(vpp, dc)
        self.set_waveform(waveform)


class OscilloscopeBase(InstrumentBase):
    @abstractmethod
    def capture_waveform(self, channel: int) -> WaveformData: ...

    @abstractmethod
    def set_timebase(self, seconds_per_div: float) -> None: ...

    @abstractmethod
    def measure(self, parameter: str, channel: int) -> float: ...

    def screenshot(self) -> bytes:
        raise NotImplementedError(f"{self.name} does not support screenshots")
