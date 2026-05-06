# teasel

Connect lab instruments to AI assistants.

teasel is an open-source platform that lets large language models talk directly to bench instruments — oscilloscopes, function generators, multimeters, power supplies — through the Model Context Protocol (MCP).

**[teasel.tools](https://teasel.tools)** — browse supported instruments

## What's in this repo

| Package | PyPI | Description |
|---|---|---|
| `teasel-cli/` | [`teasel`](https://pypi.org/project/teasel/) | CLI and TUI for configuring which instruments are available |
| `teasel-server/` | [`teasel-server`](https://pypi.org/project/teasel-server/) | MCP server that exposes instrument tools to AI assistants |

## How it works

1. Run `uvx teasel` to browse the instrument registry, enter connection details, and generate a `.mcp.json` config file.
2. Claude Code (or any MCP-compatible assistant) reads `.mcp.json` and starts `teasel-server` automatically at the beginning of each session.
3. The AI can now control and measure with your instruments directly.

## Instruments

Bundled in `teasel-server`:
- **LeCroy WaveSurfer** — oscilloscope (Ethernet / USB)
- **Philips PM5190** — LF function generator (GPIB via AR488)

Community instruments are listed in the [instruments registry](https://github.com/teasel-tools/instruments).

## Third-party drivers

Any Python package can add instruments to `teasel-server` by registering a `teasel.instruments` entry point. No changes to this repo required.

## Development

This repo is a [uv workspace](https://docs.astral.sh/uv/concepts/workspaces/). Both packages share a single venv.

**Setup:**
```bash
uv sync --all-packages
```

**Run:**
```bash
uv run teasel         # CLI / TUI
uv run teasel web     # Web UI (http://127.0.0.1:7890)
uv run teasel-server  # MCP server
```

**Test against the local instruments registry instead of GitHub:**
```bash
TEASEL_REGISTRY=/path/to/instruments uv run teasel
```

**Release:** Bump `version` in the relevant `pyproject.toml` and push to `main`. CI publishes that package to PyPI automatically via trusted publishing. Each package is released independently.

## License

AGPL v3. See [LICENSE](LICENSE).
