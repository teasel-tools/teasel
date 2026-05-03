# teasel

Connect lab instruments to AI assistants.

`teasel` is a CLI and TUI for configuring which instruments are available to your AI assistant. It browses the community instrument registry, prompts for connection details, and writes a `.mcp.json` file that Claude Code (or any MCP-compatible assistant) picks up automatically.

## Quick start

```bash
uvx teasel
```

This opens the interactive TUI. Browse instruments, select one, enter your connection details, and the config is written.

## CLI commands

```bash
# Add an instrument (prompts for required config)
uvx teasel add lecroy-wavesurfer

# Add with config inline
uvx teasel add lecroy-wavesurfer --set LECROY_HOST=192.168.1.111

# List configured instruments
uvx teasel list

# Remove an instrument
uvx teasel remove lecroy-wavesurfer

# Regenerate .mcp.json from saved state
uvx teasel apply
```

## What it generates

`teasel` writes a `.mcp.json` in the current directory:

```json
{
  "mcpServers": {
    "lab": {
      "command": "uvx",
      "args": ["teasel-server"],
      "env": {
        "LECROY_HOST": "192.168.1.111",
        "PM5190_PORT": "/dev/ttyUSB0"
      }
    }
  }
}
```

Claude Code reads this file and starts `teasel-server` automatically. You only need to run `teasel` once per project, or whenever your instrument setup changes.

## Instrument state

Configured instruments are saved to `~/.config/teasel/instruments.toml`. Running `teasel apply` in any directory regenerates `.mcp.json` from this state.

## License

AGPL v3 — see [LICENSE](https://github.com/teasel-tools/teasel/blob/main/LICENSE).
