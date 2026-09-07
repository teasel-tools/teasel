import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import httpx

DEFAULT_REGISTRY = "https://raw.githubusercontent.com/teasel-tools/instruments/main"


@dataclass
class ConfigParam:
    key: str
    description: str
    required: bool = False
    type: str = "string"
    example: str | None = None
    default: str | None = None
    discovery: str | None = None
    param: str | None = None  # friendly name used in teasel.toml


@dataclass
class IndexEntry:
    slug: str
    name: str
    manufacturer: str
    type: str
    interfaces: list[str]


@dataclass
class DriverDescriptor:
    slug: str
    name: str
    manufacturer: str
    type: str
    interfaces: list[str]
    package: str
    manual: str | None = None
    github: str | None = None
    setup_steps: list[str] = field(default_factory=list)
    params: list[ConfigParam] = field(default_factory=list)


def _registry_url() -> str:
    return os.environ.get("TEASEL_REGISTRY", DEFAULT_REGISTRY)


def _read(rel_path: str) -> bytes:
    url = _registry_url()
    if url.startswith("http"):
        resp = httpx.get(f"{url}/{rel_path}", follow_redirects=True, timeout=10)
        resp.raise_for_status()
        return resp.content
    return (Path(url) / rel_path).read_bytes()


# ── descriptor parsing (shared by bundled drivers and the remote registry) ─────

def _parse_descriptor(doc: dict) -> DriverDescriptor:
    """Parse a full instrument descriptor (the schema used by both the bundled
    drivers and the community registry: ``[instrument]`` / ``[[mcp.packages]]`` /
    ``[[mcp.config.params]]`` / ``[setup]``)."""
    inst = doc["instrument"]
    packages = doc.get("mcp", {}).get("packages", [])
    package = packages[0]["package"] if packages else "teasel-server"
    steps = [s["text"] for s in doc.get("setup", {}).get("steps", [])]
    params = [
        ConfigParam(
            key=p["key"],
            description=p["description"],
            required=p.get("required", False),
            type=p.get("type", "string"),
            example=str(p["example"]) if "example" in p else None,
            default=str(p["default"]) if "default" in p else None,
            discovery=p.get("discovery"),
            param=p.get("param"),
        )
        for p in doc.get("mcp", {}).get("config", {}).get("params", [])
    ]
    return DriverDescriptor(
        slug=inst["slug"],
        name=inst["name"],
        manufacturer=inst.get("manufacturer", ""),
        type=inst.get("type", ""),
        interfaces=inst.get("interfaces", []),
        package=package,
        manual=inst.get("manual"),
        github=inst.get("github"),
        setup_steps=steps,
        params=params,
    )


def _index_entry(doc: dict) -> IndexEntry:
    inst = doc["instrument"]
    return IndexEntry(
        slug=inst["slug"],
        name=inst["name"],
        manufacturer=inst.get("manufacturer", ""),
        type=inst.get("type", ""),
        interfaces=inst.get("interfaces", []),
    )


def _bundled_descriptors() -> dict[str, dict]:
    """Descriptors for drivers shipped inside teasel-server, if it's installed
    alongside the CLI. Empty when teasel-server isn't importable (e.g. a bare
    ``uvx teasel``) — the remote registry then covers everything."""
    try:
        from teasel_server.catalog import descriptors
        return descriptors()
    except Exception:
        return {}


# ── public API ─────────────────────────────────────────────────────────────────

def fetch_index() -> list[IndexEntry]:
    """All known instruments: bundled drivers first, then the remote registry
    (de-duped by slug). A registry fetch failure is non-fatal as long as there
    are bundled drivers to show."""
    bundled = _bundled_descriptors()
    entries = [_index_entry(doc) for doc in bundled.values()]
    seen = set(bundled)

    try:
        data = _read("index.toml")
        doc = tomllib.loads(data.decode())
        for i in doc.get("instruments", []):
            if i["slug"] in seen:
                continue
            seen.add(i["slug"])
            entries.append(
                IndexEntry(
                    slug=i["slug"],
                    name=i["name"],
                    manufacturer=i["manufacturer"],
                    type=i["type"],
                    interfaces=i.get("interfaces", []),
                )
            )
    except Exception:
        if not entries:
            raise

    return entries


def fetch_driver(slug: str) -> DriverDescriptor:
    """A driver descriptor. Bundled drivers win; otherwise the remote registry."""
    bundled = _bundled_descriptors()
    if slug in bundled:
        return _parse_descriptor(bundled[slug])

    try:
        data = _read(f"instruments/{slug}.toml")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ValueError(f"No driver found for '{slug}' in the registry")
        raise
    except FileNotFoundError:
        raise ValueError(f"No driver found for '{slug}' in the registry")
    return _parse_descriptor(tomllib.loads(data.decode()))
