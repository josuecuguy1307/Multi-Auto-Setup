"""The coordinator's configuration as data: the keys, types and defaults are `skills/os-service/references/coordinator-config.json`; the values live in `<state>/coordinator.json`. A flag on the
command line wins over the file and the file over the defaults. Nothing here knows a key by name: the schema says what exists."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from envrec_os import SKILLS_DIR

SCHEMA_FILE = SKILLS_DIR / "os-service" / "references" / "coordinator-config.json"
_TYPES = {"str": str, "int": int, "float": (int, float), "bool": bool}


class ConfigRefused(ValueError):
    """A configuration that cannot be used, with a typed reason and what to do."""

    def __init__(self, reason: str, text: str) -> None:
        super().__init__(f"{reason}: {text}")
        self.reason, self.text = reason, text


def load_schema() -> dict:
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def path_of(state: Path, schema: dict | None = None) -> Path:
    return Path(state) / (schema or load_schema())["file"]


def check(values: dict[str, Any], schema: dict | None = None) -> dict[str, Any]:
    """The values, if each key exists and has its type; a typed refusal otherwise. Unknown keys are refused, never ignored (a misspelt key would silently do nothing)."""
    keys = (schema or load_schema())["keys"]
    for k, v in values.items():
        spec = keys.get(k)
        if spec is None:
            raise ConfigRefused("unknown_key", f"{k!r} is not a coordinator setting; the settings are: {', '.join(sorted(keys))}")
        if v is None:
            if not spec.get("null_ok"):
                raise ConfigRefused("bad_value", f"{k} cannot be empty")
            continue
        want = _TYPES[spec["type"]]
        if not isinstance(v, want) or (spec["type"] != "bool" and isinstance(v, bool)):
            raise ConfigRefused("bad_type", f"{k} must be {spec['type']}, got {type(v).__name__}")
    return values


def read(state: Path, schema: dict | None = None) -> dict[str, Any]:
    """What the owner (or the installer) wrote; {} when there is no file."""
    schema = schema or load_schema()
    p = path_of(state, schema)
    if not p.is_file():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigRefused("unreadable", f"{p} is not valid JSON ({exc}); fix or remove it") from None
    if not isinstance(doc, dict):
        raise ConfigRefused("bad_shape", f"{p} must hold one JSON object")
    return check(doc, schema)


def write(state: Path, values: dict[str, Any], schema: dict | None = None) -> Path:
    """Atomic: the file is either the old one or the whole new one."""
    schema = schema or load_schema()
    check(values, schema)
    p = path_of(state, schema)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(values, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def resolve(given: dict[str, Any], state: Path, schema: dict | None = None) -> dict[str, Any]:
    """Every key with its value: what was given now (None means not given), else the file, else the default."""
    schema = schema or load_schema()
    filed = read(state, schema)
    out = {}
    for k, spec in schema["keys"].items():
        v = given.get(k)
        out[k] = v if v is not None else filed.get(k, spec.get("default"))
    return out


def from_namespace(ns: Any, schema: dict | None = None) -> dict[str, Any]:
    """The settings an argparse namespace carries (only those that were given; the parser's defaults are None)."""
    return {k: getattr(ns, k) for k in (schema or load_schema())["keys"] if getattr(ns, k, None) is not None}


def add_flags(parser: Any, schema: dict | None = None) -> None:
    """One flag per key, from the schema. The parser's own default is None so that `resolve` can tell 'not given' from 'given'."""
    for k, spec in (schema or load_schema())["keys"].items():
        kw: dict[str, Any] = {"dest": k, "default": None, "help": spec["help"] + (f" (default: {spec['default']})" if spec.get("default") is not None else "")}
        if spec["type"] == "bool":
            parser.add_argument(spec["flag"], action="store_true", **{**kw, "default": None})
            if spec.get("negate"):
                parser.add_argument(spec["negate"], action="store_false", dest=k, default=None, help=f"turn {k} off")
        else:
            parser.add_argument(spec["flag"], type={"str": str, "int": int, "float": float}[spec["type"]], **kw)
