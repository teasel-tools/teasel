import re
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import tomli_w

TEASEL_TOML = "teasel.toml"
PROJECT_TOML = "project.toml"
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


# project.toml [instruments.<slug>.limits]: numeric ceilings (amplitude_max Vpp,
# voltage_max V peak incl. DC offset, frequency_max Hz) plus an optional free-text
# `reason` that teasel-server quotes in every refusal. The server enforces them;
# the CLI/web/TUI only edit them.
LIMIT_TEXT_KEYS = ("reason",)
LIMIT_FIELDS = (
    # key, label, kind
    ("amplitude_max", "Amplitude limit (Vpp)", "number"),
    ("voltage_max", "Peak voltage limit (V, incl. DC offset)", "number"),
    ("frequency_max", "Frequency limit (Hz)", "number"),
    ("reason", "Reason (shown when a request is refused)", "text"),
)


def coerce_limits(data: dict) -> dict:
    """Normalize a [limits] table: numeric keys → float, text keys kept as str,
    unparseable numbers dropped."""
    out: dict = {}
    for k, v in (data or {}).items():
        if k in LIMIT_TEXT_KEYS:
            if str(v).strip():
                out[k] = str(v)
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def limits_from_form(values: dict[str, str], previous: dict | None = None) -> dict:
    """Build a limits dict from form field values ({key: raw string}), keeping any
    keys in ``previous`` the form doesn't know about (hand-edited project.toml)."""
    out = {k: v for k, v in (previous or {}).items() if k not in values}
    for k, raw in values.items():
        raw = (raw or "").strip()
        if not raw:
            continue
        if k in LIMIT_TEXT_KEYS:
            out[k] = raw
        else:
            try:
                out[k] = float(raw)
            except ValueError:
                pass
    return out


@dataclass
class InstrumentSetup:
    slug: str
    limits: dict[str, float | str] = field(default_factory=dict)
    channels: dict[str, dict] = field(default_factory=dict)


@dataclass
class ProviderConfig:
    """A named LLM backend bundle stored in ~/.teasel/llm.toml (holds secrets)."""
    name: str
    type: str            # cloudflare | ollama | openai-compat | anthropic
    model: str = ""      # default model for this backend
    models: list[str] = field(default_factory=list)  # selectable models (one key, many models)
    account: str = ""    # cloudflare
    token: str = ""      # cloudflare
    url: str = ""        # ollama
    base_url: str = ""   # openai-compat
    api_key: str = ""    # openai-compat / anthropic



def _toml_path(filename: str, directory: Path | None) -> Path:
    return (directory or Path.cwd()) / filename


# ── project.toml (the experiment / DUT: board, netlist, per-board probe maps,
#    and per-instrument channel/limit tuning) ─────────────────────────────────
#
# Read with tomllib, written with tomli_w. Every setter loads the whole doc,
# mutates one section, and writes it back so the rest is preserved.

def _load_project(directory: Path | None = None) -> dict:
    path = _toml_path(PROJECT_TOML, directory)
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except Exception:
        return {}


def _write_project(doc: dict, directory: Path | None = None) -> None:
    directory = directory or Path.cwd()
    path = _toml_path(PROJECT_TOML, directory)
    old_text = path.read_text() if path.exists() else ""
    new_text = "# project.toml — experiment / device under test\n\n" + tomli_w.dumps(doc)
    path.write_text(new_text)
    _append_log(old_text, new_text, directory / TEASEL_LOG, "project")


# ── Netlist (project.toml [netlist].path) ─────────────────────────────────────

def set_session_netlist(path: str) -> None:
    global _session_netlist
    _session_netlist = path


def get_netlist_path(directory: Path | None = None) -> str | None:
    """Return the active netlist path: session override > project.toml > None."""
    if _session_netlist is not None:
        return _session_netlist
    return load_netlist_path(directory)


def load_netlist_path(directory: Path | None = None) -> str | None:
    return _load_project(directory).get("netlist", {}).get("path") or None


def save_netlist_path(netlist: str | None, directory: Path | None = None) -> None:
    doc = _load_project(directory)
    if netlist:
        doc["netlist"] = {"path": netlist}
    else:
        doc.pop("netlist", None)
    _write_project(doc, directory)


# ── Project LLM provider reference (teasel.toml [llm]) ─────────────────────────

def load_project_provider(directory: Path | None = None) -> str | None:
    """Return the provider name this project pins via teasel.toml [llm].provider."""
    path = _toml_path(TEASEL_TOML, directory)
    if not path.exists():
        return None
    doc = tomllib.loads(path.read_text())
    return doc.get("llm", {}).get("provider")


def load_project_model(directory: Path | None = None) -> str | None:
    """Return the model this project pins via teasel.toml [llm].model, if any.

    Lets a single backend (one key) expose several models: the project pins both
    which backend and which of its models to use."""
    path = _toml_path(TEASEL_TOML, directory)
    if not path.exists():
        return None
    doc = tomllib.loads(path.read_text())
    return doc.get("llm", {}).get("model")


def save_project_provider(
    provider: str | None, directory: Path | None = None, model: str | None = None
) -> None:
    directory = directory or Path.cwd()
    path = _toml_path(TEASEL_TOML, directory)
    # Clearing the provider clears any pinned model with it.
    path.write_text(_serialize(
        load(directory), llm_provider=provider, llm_model=model if provider else None,
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
        llm_provider=load_project_provider(directory),
        llm_model=load_project_model(directory),
    )
    path.write_text(new_text)
    _append_log(old_text, new_text, directory / TEASEL_LOG, "instruments")


def _serialize(
    instruments: list[InstrumentConfig],
    llm_provider: str | None = None,
    llm_model: str | None = None,
) -> str:
    lines = ["# teasel.toml — lab instruments and connections", ""]
    if llm_provider:
        lines.append("[llm]")
        lines.append(f'provider = "{_escape(llm_provider)}"')
        if llm_model:
            lines.append(f'model = "{_escape(llm_model)}"')
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


# ── Per-instrument channel/limit tuning (project.toml [instruments.*]) ─────────

def load_setup(directory: Path | None = None) -> list[InstrumentSetup]:
    doc = _load_project(directory)
    return [
        InstrumentSetup(
            slug=slug,
            limits=coerce_limits(data.get("limits", {})),
            channels={ch: dict(cfg) for ch, cfg in data.get("channels", {}).items()},
        )
        for slug, data in doc.get("instruments", {}).items()
    ]


def save_setup(setups: list[InstrumentSetup], directory: Path | None = None) -> None:
    doc = _load_project(directory)
    insts: dict[str, dict] = {}
    for s in setups:
        entry: dict = {}
        if s.limits:
            entry["limits"] = coerce_limits(s.limits)
        chans = {ch: dict(cfg) for ch, cfg in s.channels.items() if cfg}
        if chans:
            entry["channels"] = chans
        if entry:
            insts[s.slug] = entry
    if insts:
        doc["instruments"] = insts
    else:
        doc.pop("instruments", None)
    _write_project(doc, directory)


# ── Loaded board (project.toml [board].file) ──────────────────────────────────

def load_board_file(directory: Path | None = None) -> str | None:
    """Return the currently-loaded board filename, or None."""
    return _load_project(directory).get("board", {}).get("file") or None


def save_board_file(filename: str | None, directory: Path | None = None) -> None:
    """Set (or clear, if None) the loaded board, preserving everything else."""
    doc = _load_project(directory)
    if filename:
        doc["board"] = {"file": filename}
    else:
        doc.pop("board", None)
    _write_project(doc, directory)


# ── Journal document (project.toml [journal].file → a Markdown file on disk) ───
#
# The journal is a real .md file beside project.toml (like the board), so it's
# prose-friendly and hand-editable outside teasel. Saving it doesn't touch
# project.toml (so it won't trip the server's setup watcher) — live sync between
# brain and browser rides the SSE journal_changed event instead.

_JOURNAL_DEFAULT = "journal.md"


def journal_filename(directory: Path | None = None) -> str:
    """Journal filename within the project dir ([journal].file, default journal.md)."""
    return _load_project(directory).get("journal", {}).get("file") or _JOURNAL_DEFAULT


def load_journal(directory: Path | None = None) -> str:
    """Return the project's journal Markdown, or '' if none yet."""
    path = (directory or Path.cwd()) / journal_filename(directory)
    try:
        return path.read_text()
    except Exception:
        return ""


def save_journal(content: str, directory: Path | None = None) -> None:
    """Write the journal Markdown; record its [journal].file reference once."""
    directory = directory or Path.cwd()
    name = journal_filename(directory)
    (directory / name).write_text(content)
    doc = _load_project(directory)
    if doc.get("journal", {}).get("file") != name:
        doc.setdefault("journal", {})["file"] = name
        _write_project(doc, directory)


# ── Per-board probe maps (project.toml [boards."<file>".probes]) ───────────────
#
# The net→channel/role map is a property of the board (DUT), so it's keyed by
# board filename. Switching boards swaps to that board's map; each is remembered.

def load_probes(board: str | None, directory: Path | None = None) -> list[dict]:
    if not board:
        return []
    boards = _load_project(directory).get("boards", {})
    return [dict(p) for p in boards.get(board, {}).get("probes", [])]


def save_probes(board: str | None, rows: list[dict], directory: Path | None = None) -> None:
    if not board:
        return
    doc = _load_project(directory)
    boards = doc.setdefault("boards", {})
    clean = [
        {
            "net": r["net"],
            "instrument": r["instrument"],
            "channel": r.get("channel", ""),
            "role": "input" if r.get("role") == "input" else "probe",
        }
        for r in rows
        if r.get("net") and r.get("instrument")
    ]
    if clean:
        boards.setdefault(board, {})["probes"] = clean
    else:
        if board in boards:
            boards[board].pop("probes", None)
            if not boards[board]:
                boards.pop(board)
    if not boards:
        doc.pop("boards", None)
    _write_project(doc, directory)


# ── UI workflow state (project.toml [ui]: mode + operator feedback) ───────────
#
# mode = standalone (web chat is the brain) | companion (Claude Code is the brain,
# web UI is a live display). feedback is a rolling log of operator notes the brain
# picks up via the server's setup-changed watcher + get_setup. Both are workflow
# state about *this experiment*, so they live in project.toml beside the probe map.

_VALID_UI_MODES = ("standalone", "companion")
_FEEDBACK_CAP = 20


def load_ui_mode(directory: Path | None = None) -> str:
    mode = _load_project(directory).get("ui", {}).get("mode", "standalone")
    return mode if mode in _VALID_UI_MODES else "standalone"


def save_ui_mode(mode: str, directory: Path | None = None) -> None:
    doc = _load_project(directory)
    doc.setdefault("ui", {})["mode"] = mode if mode in _VALID_UI_MODES else "standalone"
    _write_project(doc, directory)


def load_feedback(directory: Path | None = None) -> list[str]:
    """Operator notes (newest last), each a 'timestamp  note' string."""
    return list(_load_project(directory).get("ui", {}).get("feedback", []))


def append_feedback(note: str, directory: Path | None = None) -> None:
    """Append an operator note (rolling, capped). Writing project.toml trips the
    server's setup-changed watcher, so the brain learns about it on its next tool call."""
    note = " ".join((note or "").split()).strip()
    if not note:
        return
    doc = _load_project(directory)
    ui = doc.setdefault("ui", {})
    feedback = list(ui.get("feedback", []))
    feedback.append(f"{datetime.now().isoformat(timespec='seconds')}  {note}")
    ui["feedback"] = feedback[-_FEEDBACK_CAP:]
    _write_project(doc, directory)


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
            models=[str(m) for m in data.get("models", []) if str(m).strip()],
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
        if pc.models:
            arr = ", ".join(f'"{_escape(m)}"' for m in pc.models)
            lines.append(f"models = [{arr}]")
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
