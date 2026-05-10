import json
from importlib.metadata import distribution, version as _version


def get_version() -> str:
    ver = _version("teasel")
    try:
        direct_url = distribution("teasel").read_text("direct_url.json")
        if direct_url and json.loads(direct_url).get("dir_info", {}).get("editable"):
            ver += "+"
    except Exception:
        pass
    return ver
