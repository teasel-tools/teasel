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

## Running with a local LLM

`teasel local` connects teasel-server to a local LLM via [llama-server](https://github.com/ggerganov/llama.cpp) — no Claude or Anthropic API needed.

### 1. Get llama-server and a model

```bash
pip install llama-cpp-python[server] huggingface-hub
huggingface-cli download bartowski/Ministral-3B-Instruct-2410-GGUF \
  --include "Ministral-3B-Instruct-2410-Q8_0.gguf" --local-dir ~/models
```

### 2. Start llama-server

**Local (CPU-only):**
```bash
llama-server -m ~/models/Ministral-3B-Instruct-2410-Q8_0.gguf --jinja -ngl 0 --port 8080
```

**Rented GPU (RunPod / vast.ai — recommended for demos):**

On the pod, run the same install + download commands above, then:
```bash
llama-server -m ~/models/Ministral-3B-Instruct-2410-Q8_0.gguf --jinja -ngl 999 --port 8080 --host 0.0.0.0
```
Expose TCP port 8080 in the pod settings to get a public IP and port.

### 3. Start teasel-server in HTTP mode

```bash
uv run teasel-server --config teasel.toml --setup setup.toml --transport streamable-http --port 7891
```

### 4. Run the bridge

```bash
# Local llama-server:
uv run teasel local

# Rented GPU:
uv run teasel local --llama-url http://<pod-ip>:<port>
```

---

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
