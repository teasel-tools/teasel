import csv
import os
from datetime import datetime
from pathlib import Path

from .base import WaveformData

_output_dir = Path(os.environ.get("TEASEL_OUTPUT_DIR", "./teasel-output"))


def output_dir() -> Path:
    _output_dir.mkdir(parents=True, exist_ok=True)
    return _output_dir


def write_waveform(data: WaveformData) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:21]
    # Simulated traces carry a "node" in metadata → name them distinctly so the web
    # overlay can tell a sim trace from a bench capture.
    node = (data.metadata or {}).get("node")
    if node:
        safe = "".join(c if c.isalnum() else "_" for c in str(node))
        path = output_dir() / f"sim_{safe}_{ts}.csv"
    else:
        path = output_dir() / f"waveform_ch{data.channel}_{ts}.csv"
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time_s", f"voltage_{data.units}"])
        writer.writerows(zip(data.time_data, data.voltage_data))
    return path


def write_screenshot(slug: str, data: bytes) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:21]
    path = output_dir() / f"screenshot_{slug}_{ts}.png"
    path.write_bytes(data)
    return path
