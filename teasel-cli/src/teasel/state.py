import tomllib
from dataclasses import dataclass, field
from pathlib import Path

STATE_PATH = Path.home() / ".config" / "teasel" / "instruments.toml"


@dataclass
class InstalledInstrument:
    slug: str
    package: str
    env: dict[str, str] = field(default_factory=dict)


def load() -> list[InstalledInstrument]:
    if not STATE_PATH.exists():
        return []
    doc = tomllib.loads(STATE_PATH.read_text())
    return [
        InstalledInstrument(
            slug=i["slug"],
            package=i["package"],
            env=i.get("env", {}),
        )
        for i in doc.get("instruments", [])
    ]


def save(instruments: list[InstalledInstrument]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for inst in instruments:
        lines.append("[[instruments]]")
        lines.append(f'slug = "{inst.slug}"')
        lines.append(f'package = "{inst.package}"')
        if inst.env:
            pairs = ", ".join(f'"{k}" = "{_escape(v)}"' for k, v in inst.env.items())
            lines.append(f"env = {{ {pairs} }}")
        lines.append("")
    STATE_PATH.write_text("\n".join(lines))


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
