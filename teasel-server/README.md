# teasel-server

MCP server for lab instruments.

`teasel-server` is the [Model Context Protocol](https://modelcontextprotocol.io) server that exposes lab instruments as tools to AI assistants. It is launched automatically by Claude Code (or any MCP-compatible assistant) — you do not run it directly.

## Setup

Use the `teasel` CLI to generate the `.mcp.json` config file that tells your AI assistant how to start this server:

```bash
uvx teasel
```

See the [teasel package](https://pypi.org/project/teasel/) for full setup instructions.

## Bundled instruments

The following instruments are included out of the box:

| Instrument | Slug | Interfaces |
|---|---|---|
| LeCroy WaveSurfer | `lecroy-wavesurfer` | Ethernet (VXI-11), USB |
| Philips PM5190 | `pm5190` | GPIB (via AR488 serial adapter) |

An instrument only activates if its required environment variables are present — unconfigured instruments are invisible to the AI.

## Third-party drivers

Any Python package can add instruments without modifying `teasel-server`. Declare a `teasel.instruments` entry point in your `pyproject.toml`:

```toml
[project.entry-points."teasel.instruments"]
my-instrument = "my_package.driver:MyInstrumentDriver"
```

Inject it at runtime with `uvx`:

```bash
uvx --with my-instrument-package teasel-server
```

Or add it to the `args` list in `.mcp.json`:

```json
{
  "mcpServers": {
    "lab": {
      "command": "uvx",
      "args": ["--with", "my-instrument-package", "teasel-server"],
      "env": { "MY_INSTRUMENT_HOST": "192.168.1.50" }
    }
  }
}
```

## Writing a driver

Drivers subclass one of the base classes from `teasel_server.base`:

```python
from teasel_server.base import OscilloscopeBase, WaveformData

class MyScope(OscilloscopeBase):
    slug = "my-scope"
    name = "My Oscilloscope"

    def __init__(self, config: dict):
        host = config.get("MY_SCOPE_HOST")
        if not host:
            raise ValueError("MY_SCOPE_HOST is required")
        # connect to instrument...

    def capture_waveform(self, channel: int) -> WaveformData:
        # implementation...
```

Available base classes: `OscilloscopeBase`, `FunctionGeneratorBase`.

## License

AGPL v3 — see [LICENSE](https://github.com/teasel-tools/teasel/blob/main/LICENSE).
