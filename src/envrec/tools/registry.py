"""The tool registry: ONE registration point for every generic hand the harness (and later a skill) may use.

A tool is a small generic Python function with a typed signature and a docstring in a fixed format:

    Summary of twelve words or fewer.

    Args:
        name: what it is.
    Returns:
        what comes back (a typed result, or fact keys).
    Tier: observe | read
    Platforms: macos, linux
    Notes: anything the caller must know, or `none`.

The registry parses the docstring, refuses a tool that does not follow the format, and builds the ToolSpec the model's menu is made from.
Tools know nothing about specific devices or products. `read`-tier tools never send anything before the caller's approval function says yes.

Tools take references, never raw content (T6). The model never sees raw output, so an argument that can only be filled with raw output (a banner, a capture's text, a command's
output) is an argument nobody can fill. A tool that works on something already collected takes a `CaptureRef` (the sha256 of a stored capture, or its first 8 or more hex digits) or
a `FactRef` (a fact key); the view lists both, and CODE resolves them from the store. A tool whose input is a value a skill's script derived from a capture inside the sandbox is a
skill helper (`audience="skill"`): it is registered, documented and tested like any tool, but it is never offered on the model's menu.
"""

from __future__ import annotations

import inspect
import re
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from envrec.contracts import Origin, Tier, ToolSpec
from envrec.contracts.enums import TargetKind

SUMMARY_MAX_WORDS = 12
PROVENANCE = ("builtin", "generated")
TIERS = ("observe", "read", "write")
CTX = "ctx"
_SECTION = re.compile(r"^(Args|Returns|Tier|Platforms|Notes):\s*(.*)$")


class DocstringError(ValueError):
    pass


class ToolArgError(ValueError):
    pass


class CaptureRef(str):
    """The sha256 of a stored raw capture of the current target (or its first 8 or more hex digits). The view lists the ones that exist; code reads the bytes."""


class FactRef(str):
    """The key of a current fact of the current target (as the view lists it). Code reads the value."""


REF_TYPES = {"CaptureRef": "capture", "FactRef": "fact"}
AUDIENCES = ("model", "skill", "undo")  # "undo": the step that reverses a write; only the undo executor runs it
_HEX = re.compile(r"[0-9a-fA-F]{8,64}")


@dataclass(frozen=True)
class ArgSpec:
    name: str
    type_name: str
    required: bool
    default: Any
    doc: str

    @property
    def ref(self) -> str | None:
        """"capture" or "fact" when the argument is a reference code resolves from the store, else None (a plain value the caller writes)."""
        return REF_TYPES.get(self.type_name)


@dataclass(frozen=True)
class ParsedDoc:
    summary: str
    args: dict[str, str]
    returns: str
    tier: str
    platforms: tuple[str, ...]
    notes: str


def parse_docstring(doc: str | None, fn_name: str = "?") -> ParsedDoc:
    """Parse and validate the fixed docstring format. Raises DocstringError naming exactly what is wrong."""
    if not doc or not doc.strip():
        raise DocstringError(f"{fn_name}: a tool needs a docstring")
    lines = inspect.cleandoc(doc).splitlines()
    summary = lines[0].strip()
    words = len(summary.rstrip(".").split())
    if not summary or words > SUMMARY_MAX_WORDS:
        raise DocstringError(f"{fn_name}: the first line is the summary and must be 1 to {SUMMARY_MAX_WORDS} words (got {words})")
    sections: dict[str, list[str]] = {}
    cur = None
    for ln in lines[1:]:
        m = _SECTION.match(ln)
        if m:
            cur = m.group(1)
            if cur in sections:
                raise DocstringError(f"{fn_name}: section {cur} appears twice")
            sections[cur] = [m.group(2)] if m.group(2) else []
        elif cur is not None and ln.strip():
            sections[cur].append(ln.strip())
    missing = [s for s in ("Args", "Returns", "Tier", "Platforms", "Notes") if s not in sections]
    if missing:
        raise DocstringError(f"{fn_name}: missing section(s) {missing}")
    args: dict[str, str] = {}
    for ln in sections["Args"]:
        if ln.lower() == "none":
            continue
        name, sep, desc = ln.partition(":")
        if not sep or not desc.strip() or not re.fullmatch(r"[a-z_][a-z0-9_]*", name.strip()):
            raise DocstringError(f"{fn_name}: Args lines look like `name: description`, got {ln!r}")
        args[name.strip()] = desc.strip()
    returns = " ".join(sections["Returns"]).strip()
    tier = " ".join(sections["Tier"]).strip().lower()
    if not returns:
        raise DocstringError(f"{fn_name}: Returns is empty")
    if tier not in TIERS:
        raise DocstringError(f"{fn_name}: Tier must be one of {TIERS}, got {tier!r}")
    plats = tuple(p.strip().lower() for p in " ".join(sections["Platforms"]).split(",") if p.strip())
    if not plats or not set(plats) <= {"macos", "linux", "windows", "android", "any"}:
        raise DocstringError(f"{fn_name}: Platforms must list macos, linux, windows, android or any, got {plats}")
    notes = " ".join(sections["Notes"]).strip()
    if not notes:
        raise DocstringError(f"{fn_name}: Notes is empty (write `none` if there is nothing to say)")
    return ParsedDoc(summary.rstrip("."), args, returns, tier, plats, notes)


def _type_name(tp: Any) -> str:
    if tp is inspect.Parameter.empty:
        return "any"
    s = getattr(tp, "__name__", None) or str(tp)
    return s.replace("typing.", "")


@dataclass(frozen=True)
class Tool:
    name: str
    fn: Callable[..., Any]
    summary: str
    args: tuple[ArgSpec, ...]
    returns: str
    tier: str
    platforms: tuple[str, ...]
    notes: str
    tags: tuple[str, ...] = ()
    provenance: str = "builtin"
    ring: int = 0
    kinds: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    reducers: dict[str, str] = field(default_factory=dict, compare=False)  # capture-name prefix -> privacy reducer applied to that capture BEFORE it is stored
    approval: Callable[..., str] | None = field(default=None, compare=False)
    undo: Callable[..., dict] | None = field(default=None, compare=False)  # write tier: returns {"tool", "args", "text"}, the step that reverses this call; recorded BEFORE the effect
    repeatable: bool = False  # see ToolSpec.repeatable
    owner_thread: bool = False  # runs on the thread that owns the store (it writes facts itself); its own network/process timeouts are then what bound it
    timeout_s: float = 180.0  # the hard limit the adapter puts on one call; past it the call ends as a typed HANG, never a wait
    audience: str = "model"  # "model": on the menu. "skill": a helper a skill's script calls with values it derived from a capture inside the sandbox; never on the menu

    def applies(self, platform: str) -> bool:
        return "any" in self.platforms or platform in self.platforms

    def reducer_for(self, capture_name: str) -> str | None:
        return next((r for pre, r in self.reducers.items() if capture_name.startswith(pre)), None)

    def signature_text(self) -> str:
        return f"{self.name}(" + ", ".join(a.name + ("" if a.required else "?") for a in self.args) + ")"

    def json_schema(self) -> dict:
        props = {a.name: {"type": {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array", "dict": "object"}.get(a.type_name.split("[")[0], "string"),
                          "description": a.doc, **({"x-ref": a.ref} if a.ref else {})} for a in self.args}
        return {"type": "object", "properties": props, "required": [a.name for a in self.args if a.required], "additionalProperties": False}

    def spec(self, *, source: str = "tools") -> ToolSpec:
        """The ToolSpec the menu is made of: derived from the signature and the docstring, nothing written by hand."""
        kinds = [TargetKind(k) for k in (self.kinds or tuple(k.value for k in TargetKind))]
        tier = {"observe": Tier.observe, "read": Tier.read, "write": Tier.write_logged}[self.tier]  # a write tool asks its OWN approval, with the exact action and its undo, before it runs
        return ToolSpec(name=self.name, ring=self.ring, tier=tier, target_kinds=kinds, input_model=self.json_schema(),
                        one_line=self.summary, origin=Origin.generated if self.provenance == "generated" else Origin.builtin, source=source, verified=self.provenance == "builtin",
                        tags=list(self.tags), produces=list(self.produces), requires=list(self.requires), repeatable=self.repeatable)

    def check_args(self, given: dict[str, Any]) -> dict[str, Any]:
        names = {a.name for a in self.args}
        extra = set(given) - names
        if extra:
            raise ToolArgError(f"{self.name} has no argument(s) {sorted(extra)}; it takes {sorted(names)}")
        out = dict(given)
        for a in self.args:
            if a.ref and a.name in out:
                self._check_ref(a, out[a.name])
            if a.name not in out:
                if a.required:
                    raise ToolArgError(f"{self.name} needs the argument {a.name!r}: put it in \"args\", for example {{\"action\":\"call_tool\",\"tool\":\"{self.name}\",\"args\":{{\"{a.name}\":\"...\"}}}}")
                out[a.name] = a.default
            else:
                out[a.name] = _coerce(self.name, a, out[a.name])
        return out


    def _check_ref(self, a: ArgSpec, v: Any) -> None:
        if not isinstance(v, str) or not v.strip():
            raise ToolArgError(f"{self.name}: argument {a.name!r} must be {'a capture id from CAPTURES (the first 8 or more hex digits of its sha256)' if a.ref == 'capture' else 'a fact key from FACTS'}")
        if a.ref == "capture" and not _HEX.fullmatch(v.strip()):
            raise ToolArgError(f"{self.name}: argument {a.name!r} must be a capture id from CAPTURES (8 to 64 hex digits), not {v[:40]!r}")


def _coerce(tool: str, a: ArgSpec, v: Any) -> Any:
    t = a.type_name.split("[")[0]
    try:
        if t == "int" and not isinstance(v, bool):
            return int(v)
        if t == "float":
            return float(v)
        if t == "bool":
            if isinstance(v, bool):
                return v
            raise ValueError
        if t in ("str", "CaptureRef", "FactRef"):
            if not isinstance(v, str):
                raise ValueError
            return v
        if t == "list":
            if not isinstance(v, list):
                raise ValueError
            return v
        if t == "dict":
            if not isinstance(v, dict):
                raise ValueError
            return v
    except (TypeError, ValueError):
        raise ToolArgError(f"{tool}: argument {a.name!r} must be {a.type_name}") from None
    return v


REGISTRY: dict[str, Tool] = {}


def register_tool(t: Tool, *, approved_generated: bool = False) -> Tool:
    """The single registration point. A `generated` tool is refused unless a human-approved path passes `approved_generated=True` (not built yet)."""
    if t.provenance not in PROVENANCE:
        raise ValueError(f"provenance must be one of {PROVENANCE}")
    if t.provenance == "generated" and not approved_generated:
        raise PermissionError("generated tools are not accepted yet: the registry only takes reviewed, human-approved tools")
    if t.name in REGISTRY:
        raise ValueError(f"tool {t.name!r} is already registered")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", t.name):
        raise ValueError(f"tool name {t.name!r} must be lower snake case")
    REGISTRY[t.name] = t
    return t


def tool(*, tags: tuple[str, ...] = (), name: str | None = None, ring: int = 0, kinds: tuple[str, ...] = (), produces: tuple[str, ...] = (), requires: tuple[str, ...] = (),
         reducers: dict[str, str] | None = None, approval: Callable[..., str] | None = None, provenance: str = "builtin", audience: str = "model",
         undo: Callable[..., dict] | None = None, owner_thread: bool = False, timeout_s: float = 180.0, repeatable: bool = False):
    """Decorator: parse the docstring, check the signature against it, register the function."""
    def deco(fn):
        doc = parse_docstring(fn.__doc__, fn.__name__)
        sig = inspect.signature(fn)
        hints = typing.get_type_hints(fn)
        params = [p for p in sig.parameters.values() if p.name != CTX]
        if not sig.parameters or next(iter(sig.parameters)) != CTX:
            raise DocstringError(f"{fn.__name__}: the first parameter must be `ctx`")
        specs = []
        for p in params:
            if p.name not in doc.args:
                raise DocstringError(f"{fn.__name__}: argument {p.name!r} is not documented under Args")
            if p.annotation is inspect.Parameter.empty:
                raise DocstringError(f"{fn.__name__}: argument {p.name!r} needs a type annotation")
            specs.append(ArgSpec(p.name, _type_name(hints.get(p.name, p.annotation)), p.default is inspect.Parameter.empty, None if p.default is inspect.Parameter.empty else p.default, doc.args[p.name]))
        stale = set(doc.args) - {p.name for p in params}
        if stale:
            raise DocstringError(f"{fn.__name__}: Args documents {sorted(stale)} which are not parameters")
        if doc.tier == "write" and (approval is None or undo is None):
            raise DocstringError(f"{fn.__name__}: a write tool needs `approval` (the exact action shown to the owner) and `undo` (the step that reverses it)")
        if audience not in AUDIENCES:
            raise DocstringError(f"{fn.__name__}: audience must be one of {AUDIENCES}")
        t = Tool(name or fn.__name__, fn, doc.summary, tuple(specs), doc.returns, doc.tier, doc.platforms, doc.notes, tuple(tags), provenance, ring, tuple(kinds), tuple(produces),
                 tuple(requires), dict(reducers or {}), approval, undo, repeatable, owner_thread, timeout_s, audience)
        register_tool(t)
        fn.__tool__ = t
        return fn
    return deco


def get_tool(name: str) -> Tool | None:
    load_builtin()
    return REGISTRY.get(name)


def all_tools() -> list[Tool]:
    load_builtin()
    return sorted(REGISTRY.values(), key=lambda t: t.name)


def tool_specs(platform: str, *, tier_max: str = "read") -> list[ToolSpec]:
    """The menu material: every model-facing registered tool that applies to this platform and tier. Skill helpers (audience "skill") are never menu material."""
    ok_tiers = ("observe",) if tier_max == "observe" else TIERS
    return [t.spec() for t in all_tools() if t.applies(platform) and t.tier in ok_tiers and t.audience == "model"]


_LOADED = False


def load_builtin() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    import importlib
    import pkgutil

    from envrec.tools import builtin as pkg
    for m in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"envrec.tools.builtin.{m.name}")
