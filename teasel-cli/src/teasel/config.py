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
        env: dict[str, str] = {}
        extra_packages: list[str] = []
        seen: set[str] = set()
        for inst in instruments:
            env.update(inst.env)
            if inst.package != "teasel-server" and inst.package not in seen:
                extra_packages.append(inst.package)
                seen.add(inst.package)
        args = [item for pkg in extra_packages for item in ("--with", pkg)] + ["teasel-server"]
        servers["lab"] = {"command": "uvx", "args": args, "env": env}

    path.write_text(json.dumps(existing, indent=2) + "\n")
    return path
