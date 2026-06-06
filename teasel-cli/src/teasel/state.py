import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

TEASEL_TOML = "teasel.toml"
SETUP_TOML = "setup.toml"
TEASEL_LOG = "teasel.log"
LLM_CONFIG_PATH = Path.home() / ".teasel" / "llm.toml"

NETLIST_EXTENSIONS = frozenset({".net", ".sp", ".spice", ".asc", ".cir"})

_session_netlist: str | None = None


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


@dataclass
class ProviderConfig:
    """A named LLM backend bundle stored in ~/.teasel/llm.toml (holds secrets)."""
    name: str
    type: str            # cloudflare | ollama | openai-compat
    model: str = ""
    account: str = ""    # cloudflare
    token: str = ""      # cloudflare
    url: str = ""        # ollama
    base_url: str = ""   # openai-compat
    api_key: str = ""    # openai-compat



def _toml_path(filename: str, directory: Path | None) -> Path:
    return (directory or Path.cwd()) / filename


# ── Netlist (project section of teasel.toml) ──────────────────────────────────

def set_session_netlist(path: str) -> None:
    global _session_netlist
    _session_netlist = path


def get_netlist_path(directory: Path | None = None) -> str | None:
    """Return the active netlist path: session override > teasel.toml > None."""
    if _session_netlist is not None:
        return _session_netlist
    return load_netlist_path(directory)


def load_netlist_path(directory: Path | None = None) -> str | None:
    path = _toml_path(TEASEL_TOML, directory)
    if not path.exists():
        return None
    doc = tomllib.loads(path.read_text())
    return doc.get("project", {}).get("netlist")


def save_netlist_path(netlist: str | None, directory: Path | None = None) -> None:
    directory = directory or Path.cwd()
    path = _toml_path(TEASEL_TOML, directory)
    path.write_text(_serialize(
        load(directory), netlist=netlist, llm_provider=load_project_provider(directory)
    ))


# ── Project LLM provider reference (teasel.toml [llm]) ─────────────────────────

def load_project_provider(directory: Path | None = None) -> str | None:
    """Return the provider name this project pins via teasel.toml [llm].provider."""
    path = _toml_path(TEASEL_TOML, directory)
    if not path.exists():
        return None
    doc = tomllib.loads(path.read_text())
    return doc.get("llm", {}).get("provider")


def save_project_provider(provider: str | None, directory: Path | None = None) -> None:
    directory = directory or Path.cwd()
    path = _toml_path(TEASEL_TOML, directory)
    path.write_text(_serialize(
        load(directory), netlist=load_netlist_path(directory), llm_provider=provider
    ))


def find_netlists(directory: Path | None = None) -> list[Path]:
    d = directory or Path.cwd()
    return sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in NETLIST_EXTENSIONS
    )


# SPICE component first-letter → number of nodes
_NODE_COUNTS: dict[str, int] = {
    "R": 2, "C": 2, "L": 2, "V": 2, "I": 2, "D": 2,  # 2-terminal
    "Q": 3, "J": 3,                                      # BJT / JFET
    "M": 4, "Z": 4, "E": 4, "G": 4,                     # MOSFET / VCVS / VCCS
    "F": 2, "H": 2,                                      # CCCS / CCVS (nodes only)
}


def parse_netlist_nodes(path: str | Path) -> list[str]:
    """Extract unique node names from a SPICE netlist."""
    nodes: set[str] = set()
    for line in Path(path).read_text(errors="replace").splitlines():
        stripped = line.strip()
        # skip blank, comment, directive, and continuation lines
        if not stripped or stripped[0] in ("*", ";", ".", "+"):
            continue
        tokens = stripped.split()
        if len(tokens) < 3:
            continue
        ref = tokens[0]
        first = ref[0].upper() if ref else ""
        if first == "X":
            # subcircuit: last token is the subcircuit name, everything else is nodes
            node_toks = tokens[1:-1]
        else:
            n = _NODE_COUNTS.get(first, 2)
            node_toks = tokens[1:1 + n]
        for tok in node_toks:
            nodes.add(tok)
    return sorted(nodes, key=str.casefold)


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
    new_text = _serialize(
        instruments,
        netlist=load_netlist_path(directory),
        llm_provider=load_project_provider(directory),
    )
    path.write_text(new_text)
    _append_log(old_text, new_text, directory / TEASEL_LOG, "instruments")


def _serialize(
    instruments: list[InstrumentConfig],
    netlist: str | None = None,
    llm_provider: str | None = None,
) -> str:
    lines = ["# teasel.toml — lab instruments and connections", ""]
    if netlist is not None:
        lines.append("[project]")
        lines.append(f'netlist = "{_escape(netlist)}"')
        lines.append("")
    if llm_provider:
        lines.append("[llm]")
        lines.append(f'provider = "{_escape(llm_provider)}"')
        lines.append("")
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


# Sentinel so save_setup() preserves the existing [board] ref unless told otherwise.
_KEEP = object()


def save_setup(
    setups: list[InstrumentSetup],
    directory: Path | None = None,
    board_file: object = _KEEP,
) -> None:
    directory = directory or Path.cwd()
    path = _toml_path(SETUP_TOML, directory)
    old_text = path.read_text() if path.exists() else ""
    if board_file is _KEEP:
        board_file = load_board_file(directory)
    new_text = _serialize_setup(setups, board_file)
    path.write_text(new_text)
    _append_log(old_text, new_text, directory / TEASEL_LOG, "setup")


def load_board_file(directory: Path | None = None) -> str | None:
    """Return the project board filename recorded in setup.toml [board].file, or None."""
    path = _toml_path(SETUP_TOML, directory)
    if not path.exists():
        return None
    doc = tomllib.loads(path.read_text())
    return doc.get("board", {}).get("file") or None


def save_board_file(filename: str | None, directory: Path | None = None) -> None:
    """Set (or clear, if None) the [board].file reference, preserving everything else."""
    save_setup(load_setup(directory), directory, board_file=filename)


def _serialize_setup(setups: list[InstrumentSetup], board_file: str | None = None) -> str:
    lines = ["# setup.toml — experiment configuration (probes, limits, labels, board)", ""]
    if board_file:
        lines.append("[board]")
        lines.append(f'file = "{_escape(board_file)}"')
        lines.append("")
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


# ── LLM provider bundles (~/.teasel/llm.toml) ─────────────────────────────────
#
# Single source of truth for inference backends. Each [providers.NAME] block is a
# self-contained bundle (type + credentials + model); the top-level `default` key
# names the bundle used when nothing else is specified. Secrets live here only —
# never in a project's teasel.toml.

# Fields serialized for a bundle, in order, after the required `type`.
_PROVIDER_OPTIONAL_FIELDS = ("model", "account", "token", "url", "base_url", "api_key")


def load_providers() -> dict[str, ProviderConfig]:
    """Return all named provider bundles. Missing/old-shape file → {} (wizard handles it)."""
    if not LLM_CONFIG_PATH.exists():
        return {}
    try:
        doc = tomllib.loads(LLM_CONFIG_PATH.read_text())
    except Exception:
        return {}
    out: dict[str, ProviderConfig] = {}
    for name, data in doc.get("providers", {}).items():
        out[name] = ProviderConfig(
            name=name,
            type=str(data.get("type", "")),
            model=str(data.get("model", "")),
            account=str(data.get("account", "")),
            token=str(data.get("token", "")),
            url=str(data.get("url", "")),
            base_url=str(data.get("base_url", "")),
            api_key=str(data.get("api_key", "")),
        )
    return out


def get_default_provider() -> str | None:
    """Return the name of the default bundle, or None."""
    if not LLM_CONFIG_PATH.exists():
        return None
    try:
        return tomllib.loads(LLM_CONFIG_PATH.read_text()).get("default")
    except Exception:
        return None


def save_provider(pc: ProviderConfig, make_default: bool = False) -> None:
    """Upsert a bundle. Becomes default if asked, or if it's the first bundle."""
    providers = load_providers()
    providers[pc.name] = pc
    default = get_default_provider()
    if make_default or default is None:
        default = pc.name
    _write_providers(providers, default)


def set_default_provider(name: str) -> None:
    providers = load_providers()
    if name in providers:
        _write_providers(providers, name)


def delete_provider(name: str) -> None:
    providers = load_providers()
    if name not in providers:
        return
    del providers[name]
    default = get_default_provider()
    if default == name:
        default = next(iter(providers), None)
    _write_providers(providers, default)


def resolve_provider(
    cli_provider: str | None = None, directory: Path | None = None
) -> ProviderConfig | None:
    """Pick a bundle: --provider > teasel.toml [llm].provider > default. None → run wizard."""
    name = cli_provider or load_project_provider(directory) or get_default_provider()
    if not name:
        return None
    return load_providers().get(name)


def _write_providers(providers: dict[str, ProviderConfig], default: str | None) -> None:
    LLM_CONFIG_PATH.parent.mkdir(exist_ok=True)
    lines: list[str] = []
    if default:
        lines.append(f'default = "{_escape(default)}"')
        lines.append("")
    for name, pc in providers.items():
        lines.append(f"[providers.{name}]")
        lines.append(f'type = "{_escape(pc.type)}"')
        for fld in _PROVIDER_OPTIONAL_FIELDS:
            val = getattr(pc, fld)
            if val:
                lines.append(f'{fld} = "{_escape(str(val))}"')
        lines.append("")
    LLM_CONFIG_PATH.write_text("\n".join(lines))


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
