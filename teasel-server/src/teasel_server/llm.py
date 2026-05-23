"""Optional LLM client for teasel-server — pre-processes heavy instrument data."""

import base64
import httpx


class LLMClient:
    def __init__(self, base_url: str, model: str, headers: dict | None = None):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._http = httpx.Client(headers=headers or {}, timeout=60.0)

    def _complete(self, messages: list) -> str:
        resp = self._http.post(
            f"{self._base_url}/chat/completions",
            json={"model": self._model, "messages": messages, "stream": False},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def summarize_waveform(self, stats: dict) -> str:
        ch = stats["channel"]
        vpp = stats["v_max"] - stats["v_min"]
        prompt = (
            f"Oscilloscope channel {ch}: {stats['points']} samples at "
            f"{stats['sample_rate_hz']/1e3:.0f} kHz SR, {vpp:.3f} Vpp "
            f"({stats['v_min']:.3f}V to {stats['v_max']:.3f}V), mean {stats['v_mean']:.3f}V. "
            "In one concise sentence, describe this waveform signal."
        )
        return self._complete([{"role": "user", "content": prompt}])

    def describe_screenshot(self, png_bytes: bytes) -> str:
        b64 = base64.b64encode(png_bytes).decode()
        return self._complete([
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": "Describe what you see on this oscilloscope screen concisely. Include channel readings, measurements, and time/voltage scales visible."},
                ],
            }
        ])

    def close(self) -> None:
        self._http.close()
