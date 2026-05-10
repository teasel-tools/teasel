import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

TEASEL_TOML = "teasel.toml"
SETUP_TOML = "setup.toml"
TEASEL_LOG = "teasel.log"


@dataclass
class InstrumentConfig:
    slug: str       # instance name — user-chosen, section key in teasel.toml
    package: str
    driver: str = ""  # driver type slug for entry-point lookup; empty means same as slug
    type: str = ""
    params: dict[str, str] = field(default_factory=dict)

    @property
    def driver_slug(self) -> str:
        """The registry slug used to look up the driver entry point."""
        return self.driver or self.slug


@dataclass
class InstrumentSetup:
    slug: str
    limits: dict[str, float] = field(default_factory=dict)
    channels: dict[str, dict] = field(default_factory=dict)



def _toml_path(filename: str, directory: Path | None) -> Path:
    return (directory or Path.cwd()) / filename


# ── Instrument connections (teasel.toml) ──────────────────────────────────────

def load(directory: Path | None = None) -> list[InstrumentConfig]:
    path = _toml_path(TEASEL_TOML, directory)
    if not path.exists():
        return []
    doc = tomllib.loads(path.read_text())
    return [
        InstrumentConfig(
            slug=slug,
            package=data.get("package", "teasel-server"),
            driver=data.get("driver", ""),
            type=data.get("type", ""),
            params={
                k: str(v)
                for k, v in data.items()
                if k not in ("package", "driver", "type")
            },
        )
        for slug, data in doc.get("instruments", {}).items()
    ]


def save(instruments: list[InstrumentConfig], directory: Path | None = None) -> None:
    directory = directory or Path.cwd()
    path = _toml_path(TEASEL_TOML, directory)
    old_text = path.read_text() if path.exists() else ""
    new_text = _serialize(instruments)
    path.write_text(new_text)
    _append_log(old_text, new_text, directory / TEASEL_LOG, "instruments")


def _serialize(instruments: list[InstrumentConfig]) -> str:
    lines = ["# teasel.toml — lab instruments and connections", ""]
    for inst in instruments:
        lines.append(f"[instruments.{inst.slug}]")
        if inst.driver and inst.driver != inst.slug:
            lines.append(f'driver = "{inst.driver}"')
        if inst.package != "teasel-server":
            lines.append(f'package = "{inst.package}"')
        if inst.type:
            lines.append(f'type = "{inst.type}"')
        for k, v in inst.params.items():
            lines.append(_toml_value(k, v))
        lines.append("")
    return "\n".join(lines)


# ── Experiment setup (setup.toml) ─────────────────────────────────────────────

def load_setup(directory: Path | None = None) -> list[InstrumentSetup]:
    path = _toml_path(SETUP_TOML, directory)
    if not path.exists():
        return []
    doc = tomllib.loads(path.read_text())
    return [
        InstrumentSetup(
            slug=slug,
            limits={k: float(v) for k, v in data.get("limits", {}).items()},
            channels={ch: dict(cfg) for ch, cfg in data.get("channels", {}).items()},
        )
        for slug, data in doc.get("instruments", {}).items()
    ]


def save_setup(setups: list[InstrumentSetup], directory: Path | None = None) -> None:
    directory = directory or Path.cwd()
    path = _toml_path(SETUP_TOML, directory)
    old_text = path.read_text() if path.exists() else ""
    new_text = _serialize_setup(setups)
    path.write_text(new_text)
    _append_log(old_text, new_text, directory / TEASEL_LOG, "setup")


def _serialize_setup(setups: list[InstrumentSetup]) -> str:
    lines = ["# setup.toml — experiment configuration (probes, limits, labels)", ""]
    for s in setups:
        if s.limits:
            lines.append(f"[instruments.{s.slug}.limits]")
            for k, v in s.limits.items():
                lines.append(f"{k} = {v}")
            lines.append("")
        for ch, cfg in s.channels.items():
            lines.append(f"[instruments.{s.slug}.channels.{ch}]")
            for k, v in cfg.items():
                lines.append(_toml_value(k, str(v)))
            lines.append("")
    return "\n".join(lines)


# ── Instance naming ───────────────────────────────────────────────────────────

def next_instance_name(driver_slug: str) -> str:
    """Return the next available instance name for a driver (pm5190, pm5190-2, pm5190-3…)."""
    taken = {i.slug for i in load()}
    if driver_slug not in taken:
        return driver_slug
    n = 2
    while f"{driver_slug}-{n}" in taken:
        n += 1
    return f"{driver_slug}-{n}"


# ── Shared helpers ────────────────────────────────────────────────────────────

def _toml_value(k: str, v: str) -> str:
    try:
        num = int(v) if "." not in str(v) else float(v)
        return f"{k} = {num}"
    except (ValueError, TypeError):
        return f'{k} = "{_escape(str(v))}"'


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _append_log(old_text: str, new_text: str, log_path: Path, section: str) -> None:
    if old_text == new_text:
        return
    try:
        old_doc = tomllib.loads(old_text) if old_text.strip() else {}
    except Exception:
        old_doc = {}
    try:
        new_doc = tomllib.loads(new_text) if new_text.strip() else {}
    except Exception:
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entries: list[str] = []

    old_insts = old_doc.get("instruments", {})
    new_insts = new_doc.get("instruments", {})

    for slug in set(new_insts) - set(old_insts):
        entries.append(f"{ts}  [{section}] added {slug}")
    for slug in set(old_insts) - set(new_insts):
        entries.append(f"{ts}  [{section}] removed {slug}")

    for slug, new_data in new_insts.items():
        old_data = old_insts.get(slug, {})
        _diff_dict(old_data, new_data, f"{slug}", ts, entries)

    if entries:
        with log_path.open("a") as f:
            f.write("\n".join(entries) + "\n")


def _diff_dict(old: dict, new: dict, prefix: str, ts: str, entries: list[str]) -> None:
    for k, v in new.items():
        if isinstance(v, dict):
            _diff_dict(old.get(k, {}), v, f"{prefix}.{k}", ts, entries)
        else:
            old_v = old.get(k)
            if old_v is not None and str(old_v) != str(v):
                entries.append(f"{ts}  {prefix}.{k}: {old_v} → {v}")
