import os
import tomllib
from pathlib import Path

import httpx

from .models import ConfigParam, IndexEntry, InstrumentDescriptor, McpPackage, RegistryConfig

DEFAULT_REGISTRY = RegistryConfig(
    name="teasel",
    url="https://raw.githubusercontent.com/teasel-tools/instruments/main",
)
CONFIG_PATH = Path.home() / ".config" / "teasel" / "config.toml"


def load_registries() -> list[RegistryConfig]:
    # TEASEL_REGISTRY env var overrides everything — single registry, quick testing
    env_override = os.environ.get("TEASEL_REGISTRY")
    if env_override:
        return [RegistryConfig(name="teasel", url=env_override)]

    if not CONFIG_PATH.exists():
        return [DEFAULT_REGISTRY]

    doc = tomllib.loads(CONFIG_PATH.read_text())
    registries = [RegistryConfig(name=r["name"], url=r["url"]) for r in doc.get("registries", [])]
    if not any(r.url == DEFAULT_REGISTRY.url for r in registries):
        registries.append(DEFAULT_REGISTRY)
    return registries or [DEFAULT_REGISTRY]


def _read(url: str, rel_path: str) -> bytes:
    if url.startswith("http"):
        with httpx.Client() as client:
            resp = client.get(f"{url}/{rel_path}", follow_redirects=True)
            resp.raise_for_status()
            return resp.content
    return (Path(url) / rel_path).read_bytes()


def fetch_index() -> tuple[list[IndexEntry], list[str]]:
    """Returns (entries, warnings) where warnings lists any unreachable registries."""
    registries = load_registries()
    seen: set[str] = set()
    entries: list[IndexEntry] = []
    warnings: list[str] = []
    for reg in registries:
        try:
            for entry in _parse_index(_read(reg.url, "index.toml"), reg):
                if entry.slug not in seen:
                    seen.add(entry.slug)
                    entries.append(entry)
        except Exception as e:
            warnings.append(f"Could not reach registry '{reg.name}' ({reg.url}): {e}")
    return entries, warnings


def fetch_instrument(entry: IndexEntry) -> InstrumentDescriptor:
    return _parse_instrument(_read(entry.registry_url, entry.file))


def fetch_instrument_by_slug(slug: str) -> tuple[InstrumentDescriptor, str, list[str]]:
    """Returns (descriptor, registry_name, warnings)."""
    entries, warnings = fetch_index()
    entry = next((e for e in entries if e.slug == slug), None)
    if entry is None:
        raise ValueError(f"Unknown instrument slug: '{slug}'")
    return fetch_instrument(entry), entry.registry, warnings


def _parse_index(data: bytes, reg: RegistryConfig) -> list[IndexEntry]:
    doc = tomllib.loads(data.decode())
    return [
        IndexEntry(
            slug=i["slug"],
            name=i["name"],
            manufacturer=i["manufacturer"],
            type=i["type"],
            interfaces=i.get("interfaces", []),
            file=i["file"],
            registry=reg.name,
            registry_url=reg.url,
        )
        for i in doc.get("instruments", [])
    ]


def _parse_instrument(data: bytes) -> InstrumentDescriptor:
    doc = tomllib.loads(data.decode())
    inst = doc["instrument"]
    packages = [
        McpPackage(
            package=p["package"],
            distribution=p.get("distribution", "pypi"),
            install=p["install"],
            command=p["command"],
            description=p.get("description", ""),
            author=p.get("author", ""),
            language=p.get("language", ""),
            url=p.get("url", ""),
        )
        for p in doc.get("mcp", {}).get("packages", [])
    ]
    params = [
        ConfigParam(
            key=p["key"],
            description=p["description"],
            required=p.get("required", False),
            type=p.get("type", "string"),
            example=str(p["example"]) if "example" in p else None,
            default=str(p["default"]) if "default" in p else None,
        )
        for p in doc.get("mcp", {}).get("config", {}).get("params", [])
    ]
    setup_steps = [s["text"] for s in doc.get("setup", {}).get("steps", [])]
    return InstrumentDescriptor(
        slug=inst["slug"],
        name=inst["name"],
        manufacturer=inst["manufacturer"],
        type=inst["type"],
        interfaces=inst.get("interfaces", []),
        packages=packages,
        params=params,
        year=inst.get("year"),
        manual=inst.get("manual"),
        image=inst.get("image"),
        github=inst.get("github"),
        setup_steps=setup_steps,
    )
