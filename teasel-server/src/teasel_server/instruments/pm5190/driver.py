from teasel_server.base import FunctionGeneratorBase
from .instrument import PM5190, PM5190Error


class PM5190Driver(FunctionGeneratorBase):
    waveforms = ["sine", "square", "triangle", "sine/AM ext", "triangle/AM ext"]
    PARAM_MAP = {"port": "PM5190_PORT", "addr": "PM5190_ADDR", "baud": "PM5190_BAUD"}

    @property
    def slug(self) -> str:
        return "pm5190"

    @property
    def name(self) -> str:
        return "Philips PM5190"

    _waveform_map = {
        "sine": 1,
        "square": 2,
        "triangle": 3,
        "sine/AM ext": 4,
        "triangle/AM ext": 5,
    }

    def __init__(self, config: dict):
        port = config.get("PM5190_PORT")
        if not port:
            raise ValueError("PM5190_PORT is required")
        baud = int(config.get("PM5190_BAUD", 115200))
        addr = int(config.get("PM5190_ADDR", 4))
        self._gen = PM5190()
        self._gen.connect(port, baud, addr)

    def status(self) -> str:
        if self._gen.is_connected:
            return f"{self.name} connected on {self._gen.port} — firmware: {self._gen.firmware}"
        return f"{self.name} not connected"

    def set_frequency(self, freq_hz: float) -> None:
        self._gen.set_frequency(freq_hz)

    def set_amplitude(self, vpp: float, dc: float = 0.0) -> None:
        self._gen.set_amplitude(vpp, dc)

    def set_waveform(self, waveform: str) -> None:
        code = self._waveform_map.get(waveform)
        if code is None:
            raise PM5190Error(f"Unknown waveform '{waveform}', must be one of: {list(self._waveform_map)}")
        self._gen.set_waveform(code)

    def configure(self, freq_hz: float, vpp: float, dc: float = 0.0, waveform: str = "sine") -> None:
        code = self._waveform_map.get(waveform)
        if code is None:
            raise PM5190Error(f"Unknown waveform '{waveform}'")
        self._gen.configure(freq_hz, vpp, dc, code)
