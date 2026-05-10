import json
from pathlib import Path

from .state import InstrumentConfig, SETUP_TOML


def apply(instruments: list[InstrumentConfig], output_dir: Path | None = None) -> Path:
    output_dir = output_dir or Path.cwd()
    mcp_path = output_dir / ".mcp.json"
    config_path = (output_dir / "teasel.toml").resolve()
    setup_path = (output_dir / SETUP_TOML).resolve()

    existing: dict = {}
    if mcp_path.exists():
        try:
            existing = json.loads(mcp_path.read_text())
        except json.JSONDecodeError:
            pass

    servers = existing.setdefault("mcpServers", {})

    if not instruments:
        servers.pop("lab", None)
    else:
        extra_packages: list[str] = []
        seen: set[str] = set()
        for inst in instruments:
            if inst.package != "teasel-server" and inst.package not in seen:
                extra_packages.append(inst.package)
                seen.add(inst.package)

        args = [item for pkg in extra_packages for item in ("--with", pkg)]
        args += ["teasel-server", "--config", str(config_path), "--setup", str(setup_path)]
        servers["lab"] = {"command": "uvx", "args": args}

    mcp_path.write_text(json.dumps(existing, indent=2) + "\n")
    return mcp_path
