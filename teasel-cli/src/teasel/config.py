import json
from pathlib import Path

from .state import InstalledInstrument


def apply(instruments: list[InstalledInstrument], output_dir: Path | None = None) -> Path:
    output_dir = output_dir or Path.cwd()
    path = output_dir / ".mcp.json"

    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError:
            pass

    servers = existing.setdefault("mcpServers", {})

    if not instruments:
        servers.pop("lab", None)
    else:
        args: list[str] = []
        env: dict[str, str] = {}
        for inst in instruments:
            args += ["--with", inst.package]
            env.update(inst.env)
        args.append("teasel-server")
        servers["lab"] = {"command": "uvx", "args": args, "env": env}

    path.write_text(json.dumps(existing, indent=2) + "\n")
    return path
