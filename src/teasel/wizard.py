from .config import make_resolved_config
from .models import ConfigParam, InstrumentDescriptor, ResolvedConfig


class MissingRequiredParam(Exception):
    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        super().__init__(f"Missing required params: {', '.join(keys)}")


class InvalidParamValue(Exception):
    def __init__(self, key: str, value: str, expected_type: str) -> None:
        self.key = key
        self.value = value
        self.expected_type = expected_type
        super().__init__(f"'{value}' is not a valid {expected_type} for {key}")


def parse_set_args(set_args: list[str]) -> dict[str, str]:
    result = {}
    for item in set_args:
        if "=" not in item:
            raise ValueError(f"Invalid --set format: '{item}'. Expected KEY=VALUE.")
        key, _, value = item.partition("=")
        result[key.strip()] = value
    return result


def validate_and_coerce(param: ConfigParam, raw_value: str) -> str:
    t = param.type
    if t == "integer":
        try:
            int(raw_value)
        except ValueError:
            raise InvalidParamValue(param.key, raw_value, "integer")
    elif t == "float":
        try:
            float(raw_value)
        except ValueError:
            raise InvalidParamValue(param.key, raw_value, "float")
    elif t == "boolean":
        if raw_value.lower() not in ("true", "false", "1", "0"):
            raise InvalidParamValue(param.key, raw_value, "boolean")
    return raw_value


def resolve_values(
    params: list[ConfigParam],
    provided: dict[str, str],
) -> dict[str, str]:
    resolved = {}
    for param in params:
        value = provided.get(param.key) or param.default
        if value is not None:
            resolved[param.key] = value
    return resolved


def check_required(
    params: list[ConfigParam],
    resolved: dict[str, str],
) -> list[str]:
    return [p.key for p in params if p.required and p.key not in resolved]


def build_resolved_config(
    instrument: InstrumentDescriptor,
    provided: dict[str, str],
    package_index: int = 0,
) -> ResolvedConfig:
    resolved = resolve_values(instrument.params, provided)
    missing = check_required(instrument.params, resolved)
    if missing:
        raise MissingRequiredParam(missing)
    return make_resolved_config(instrument, resolved, package_index)
