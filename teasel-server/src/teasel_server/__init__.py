import json
import subprocess
from importlib.metadata import distribution, version as _version
from pathlib import Path


def get_version() -> str:
    """Package version, with a git suffix for dev (editable) installs.

    Published installs return the bare version (e.g. ``0.1.8``). Editable
    installs append the abbreviated commit of the source checkout in PEP 440
    local-version syntax, marked ``.dirty`` when the working tree has
    uncommitted changes (e.g. ``0.1.8+g5c82d51.dirty``). Falls back to
    ``+editable`` when the source isn't a reachable git checkout.
    """
    ver = _version("teasel-server")
    if _is_editable("teasel-server"):
        ver += "+" + (_git_revision(Path(__file__).parent) or "editable")
    return ver


def _is_editable(dist_name: str) -> bool:
    try:
        direct_url = distribution(dist_name).read_text("direct_url.json")
        return bool(direct_url and json.loads(direct_url).get("dir_info", {}).get("editable"))
    except Exception:
        return False


def _git_revision(start: Path) -> str | None:
    """``g<short-hash>`` (+ ``.dirty``) for the git repo containing ``start``."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=start, capture_output=True, text=True, timeout=2,
        )
        if head.returncode != 0:
            return None
        rev = "g" + head.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=start, capture_output=True, text=True, timeout=2,
        )
        if dirty.stdout.strip():
            rev += ".dirty"
        return rev
    except Exception:
        return None
