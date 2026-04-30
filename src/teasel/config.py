import json
import shlex
from collections.abc import Callable
from pathlib import Path

from .models import InstrumentDescriptor, ResolvedConfig

WriterFn = Callable[[ResolvedConfig, Path], None]
WRITERS: dict[str, WriterFn] = {}


def split_command(raw: str) -> tuple[str, list[str]]:
    parts = shlex.split(raw)
    return parts[0], parts[1:]


def make_resolved_config(
    instrument: InstrumentDescriptor,
    values: dict[str, str],
    package_index: int = 0,
) -> ResolvedConfig:
    pkg = instrument.packages[package_index]
    command, args = split_command(pkg.command)
    return ResolvedConfig(
        slug=instrument.slug,
        command=command,
        args=args,
        env={k: v for k, v in values.items() if v},
    )


def write_claude_code(config: ResolvedConfig, path: Path) -> None:
    existing: dict = {}
    if path.exists():
        with path.open() as f:
            existing = json.load(f)
    existing.setdefault("mcpServers", {})[config.slug] = {
        "command": config.command,
        "args": config.args,
        "env": config.env,
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
        f.write("\n")


WRITERS["claude-code"] = write_claude_code


def write_config(
    config: ResolvedConfig,
    target: str = "claude-code",
    output_dir: Path | None = None,
) -> Path:
    if target not in WRITERS:
        raise ValueError(f"Unknown config target: '{target}'. Available: {list(WRITERS)}")
    output_dir = output_dir or Path.cwd()
    target_paths = {
        "claude-code": output_dir / ".mcp.json",
    }
    path = target_paths[target]
    WRITERS[target](config, path)
    return path
