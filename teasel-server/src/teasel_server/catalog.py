"""Catalog of the drivers bundled in teasel-server.

Every bundled driver ships a descriptor TOML co-located with its code (the same
schema the community registry uses — ``[instrument]`` / ``[[mcp.packages]]`` /
``[[mcp.config.params]]`` / ``[setup]``). This module discovers those files via
the ``teasel.instruments`` entry points, so a bundled driver is fully
self-describing and needs no entry in the external registry.

teasel-cli imports this (optionally) so ``teasel add`` / the picker / the web
catalog can list and configure bundled drivers offline.
"""

from importlib.metadata import entry_points
from importlib.resources import files

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py3.10 fallback
    import tomli as tomllib  # type: ignore


def _descriptor_for(slug: str, module: str) -> dict | None:
    """Read the descriptor TOML sitting next to a driver module.

    Looks for ``{slug}.toml`` first (lets one package host several drivers, e.g.
    the dummies) then a generic ``instrument.toml``.
    """
    pkg = module.rsplit(".", 1)[0]  # driver module's package dir
    try:
        anchor = files(pkg)
    except (ModuleNotFoundError, TypeError):
        return None
    for candidate in (f"{slug}.toml", "instrument.toml"):
        path = anchor / candidate
        if path.is_file():
            return tomllib.loads(path.read_text(encoding="utf-8"))
    return None


def descriptors() -> dict[str, dict]:
    """Map slug → parsed descriptor doc for every bundled driver that ships one."""
    out: dict[str, dict] = {}
    for ep in entry_points(group="teasel.instruments"):
        module = ep.value.split(":", 1)[0]
        doc = _descriptor_for(ep.name, module)
        if doc is not None:
            out[ep.name] = doc
    return out
