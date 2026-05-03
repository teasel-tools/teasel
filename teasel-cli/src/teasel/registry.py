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


def fetch_index() -> list[IndexEntry]:
    data = _read("index.toml")
    doc = tomllib.loads(data.decode())
    return [
        IndexEntry(
            slug=i["slug"],
            name=i["name"],
            manufacturer=i["manufacturer"],
            type=i["type"],
            interfaces=i.get("interfaces", []),
        )
        for i in doc.get("instruments", [])
    ]


def fetch_driver(slug: str) -> DriverDescriptor:
    try:
        data = _read(f"drivers/{slug}.toml")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise ValueError(f"No driver found for '{slug}' in the registry")
        raise
    except FileNotFoundError:
        raise ValueError(f"No driver found for '{slug}' in the registry")
    doc = tomllib.loads(data.decode())
    inst = doc["instrument"]
    drv = doc["driver"]
    steps = [s["text"] for s in doc.get("setup", {}).get("steps", [])]
    params = [
        ConfigParam(
            key=p["key"],
            description=p["description"],
            required=p.get("required", False),
            type=p.get("type", "string"),
            example=str(p["example"]) if "example" in p else None,
            default=str(p["default"]) if "default" in p else None,
        )
        for p in doc.get("config", {}).get("params", [])
    ]
    return DriverDescriptor(
        slug=inst["slug"],
        name=inst["name"],
        manufacturer=inst.get("manufacturer", ""),
        type=inst.get("type", ""),
        interfaces=inst.get("interfaces", []),
        package=drv["package"],
        manual=inst.get("manual"),
        github=inst.get("github"),
        setup_steps=steps,
        params=params,
    )
