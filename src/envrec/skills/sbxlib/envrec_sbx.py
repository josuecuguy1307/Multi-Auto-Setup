"""What a skill's script may import inside the sandbox. Standard library only.

A script defines `parse(inp)` and returns `Out(...).result()`. It receives the captures that registered tools collected (`inp["captures"]`) and returns facts,
entities and unknowns that cite a capture (`src`). It cannot run anything: when it needs another read it ASKS (`out.want(...)`) and the harness decides."""

from __future__ import annotations

import json
import re

GB = 1000**3
GIB = 2**30
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f  ​-‏‪-‮⁦-⁩]")
_ID_PATTERNS = (re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b"),
                re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"), re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{12,16}(?![0-9A-Fa-f])"))


def clean_text(value, cap=80):
    s = _ANSI.sub("", str(value))
    s = _CTRL.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= cap else s[: cap - 1] + "…"


def mask_ids(value, cap=80):
    s = clean_text(value, cap * 2)
    for p in _ID_PATTERNS:
        s = p.sub("<id>", s)
    return clean_text(s, cap)


def first_version(text):
    m = re.search(r"\d+(?:\.\d+)+(?:[-+.\w]*)?", text)
    return m.group(0)[:40] if m else None


def num(value):
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(value)))
    except ValueError:
        return None


class Cap:
    def __init__(self, name, d):
        self.name, self.text, self.ok = name, d.get("text", ""), bool(d.get("ok"))
        self.error, self.detail = d.get("error"), d.get("detail", "")

    def json(self):
        if not self.ok:
            return None
        try:
            return json.loads(self.text)
        except ValueError:
            return None

    def lines(self):
        return self.text.splitlines()


class Caps:
    pending: list = []

    def __init__(self, inp):
        self.pending = []
        self.platform = inp.get("platform", "")
        self.params = inp.get("params", {})
        self.round = inp.get("round", 0)
        self._c = {k: Cap(k, v) for k, v in inp.get("captures", {}).items()}

    def get(self, name):
        return self._c.get(name)

    def __contains__(self, name):
        return name in self._c

    def names(self):
        return list(self._c)

    def with_prefix(self, prefix):
        return [c for n, c in self._c.items() if n.startswith(prefix)]

    @property
    def first_ok(self):
        return next((n for n, c in self._c.items() if c.ok), None)


class Out:
    """Builder for the one thing a script returns."""

    def __init__(self, caps=None):
        self.caps = caps
        self.facts, self.entities, self.unknowns, self.calls = [], [], [], []

    def _src(self, src):
        return src or (self.caps.first_ok if self.caps else None)

    def fact(self, key, value, src=None, limitations=None, confidence=0.9, hypothesis=False, quote=None):
        self.facts.append({"key": key, "value": value, "src": self._src(src), "limitations": list(limitations or []), "confidence": confidence, "hypothesis": hypothesis, "quote": quote})
        return self

    def entity(self, kind, ring, ident, owned="unknown", attrs=None, resolved=True, src=None, prefix=None, hypothesis=False, confidence=0.9, links=None, quote=None):
        self.entities.append({"kind": kind, "ring": ring, "ident": str(ident), "owned": owned, "attrs": attrs or {}, "resolved": bool(resolved), "src": self._src(src), "prefix": prefix,
                              "hypothesis": hypothesis, "confidence": confidence, "links": [list(x) for x in (links or [])], "quote": quote})
        return self

    def unknown(self, key, reason, detail):
        self.unknowns.append({"key": key, "reason": reason, "detail": str(detail)[:200]})
        return self

    def lost(self, cap, *keys):
        """Unknown facts for a capture that failed or is missing, with its typed reason."""
        reason = (cap.error if cap is not None and cap.error else None) or "unavailable"
        detail = cap.detail if cap is not None else "no capture"
        for k in keys:
            self.unknown(k, reason, detail)
        return self

    def want(self, id, tool, args=None, reducer=None):
        """Ask the harness for another read by a registered tool. It runs only if the tool exists, the arguments fit and the tier is allowed."""
        self.calls.append({"id": id, "tool": tool, "args": args or {}, "reducer": reducer})
        return self

    def result(self):
        return {"facts": self.facts, "entities": self.entities, "unknowns": self.unknowns, "next": self.calls}


# ---- the shapes a ported parser returns, and the bridge to the one answer a script gives ---------------------------------------------------

from dataclasses import dataclass, field  # noqa: E402


class Reason:
    unavailable = "unavailable"
    denied = "denied"
    timeout = "timeout"
    not_applicable = "not_applicable"
    permission = "permission"


@dataclass
class FactOut:
    key: str
    value: object
    confidence: float = 0.9
    limitations: list = field(default_factory=list)
    src: str | None = None
    hypothesis: bool = False


@dataclass
class UnknownOut:
    key: str
    reason: str
    detail: str = ""


@dataclass
class Entity:
    kind: str
    ring: int
    ident: str
    owned: str = "unknown"
    attrs: dict = field(default_factory=dict)
    resolved: bool = True
    prefix: str | None = None
    src: str | None = None
    hypothesis: bool = False
    confidence: float = 0.9
    links: list = field(default_factory=list)


def by_name(captures):
    return {c.name: c for c in captures}


def json_of(c):
    return c.json() if c is not None else None


def lost(c, *keys):
    """Unknown facts for a capture that failed or is missing, with its typed reason."""
    reason = (c.error if c is not None and c.error else None) or "unavailable"
    detail = c.detail if c is not None else "no capture"
    return [UnknownOut(k, reason, detail) for k in keys]


def run_parser(inp, fn):
    """Call `fn(captures, platform, caps)` and turn the typed results it returns into the answer of the script. `fn` returns a list of FactOut, Entity and UnknownOut."""
    caps = Caps(inp)
    outs = fn(list(caps._c.values()), caps.platform, caps)
    return to_result(outs, caps)


def to_result(outs, caps):
    out = Out(caps)
    if getattr(caps, "pending", None):
        for cid, tool, args in caps.pending:
            out.want(cid, tool, args)
        return out.result()
    for o in outs:
        if isinstance(o, FactOut):
            out.fact(o.key, o.value, src=o.src, limitations=o.limitations, confidence=o.confidence, hypothesis=o.hypothesis)
        elif isinstance(o, Entity):
            out.entity(o.kind, o.ring, o.ident, o.owned, o.attrs, o.resolved, o.src, o.prefix, o.hypothesis, o.confidence, o.links)
        elif isinstance(o, UnknownOut):
            out.unknown(o.key, o.reason, o.detail)
    return out.result()


class Lookups:
    """Reference-table lookups are registered tools. A script that needs one asks (`pending`); the harness runs it and calls the script again with the answer, so the
    script reads the table's answer from a capture and never opens the table itself. Until the answer is there a lookup returns None and the round is a request only."""

    def __init__(self, caps):
        self.caps = caps
        caps.pending = getattr(caps, "pending", [])

    def _ask(self, cid, tool, args):
        c = self.caps.get(cid)
        if c is None:
            if not any(p[0] == cid for p in self.caps.pending):
                self.caps.pending.append((cid, tool, args))
            return None
        return c

    def call(self, cid, tool, args):
        """The `data` of a lookup, a dict; {} when the tool failed (a missing table); None while the answer is still being fetched."""
        c = self._ask(cid, tool, args)
        if c is None:
            return None
        if not c.ok:
            return {"_failed": True, "_error": c.error, "_detail": c.detail}
        try:
            return json.loads(c.text)
        except ValueError:
            return {"_failed": True, "_error": "unavailable", "_detail": "not JSON"}
