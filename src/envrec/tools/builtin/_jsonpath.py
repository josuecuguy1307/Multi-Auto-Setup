"""A small JSONPath subset ($.a.b[0], [*]) for the json_path tool. Nothing here evaluates code."""

from __future__ import annotations

import re
from typing import Any


class ExtractError(ValueError):
    pass


_TOKEN = re.compile(r"\.([A-Za-z0-9_\-:]+)|\[(\*|-?\d+)\]|\['([^']*)'\]")


class ExtractError(ValueError):
    pass


def parse_path(path: str) -> list[str | int]:
    if not path.startswith("$"):
        raise ExtractError(f"a path starts with $, got {path!r}")
    rest, out, pos = path[1:], [], 0
    while pos < len(rest):
        m = _TOKEN.match(rest, pos)
        if not m:
            raise ExtractError(f"cannot read the path {path!r} at {rest[pos:]!r}")
        pos = m.end()
        if m.group(1) is not None:
            out.append(m.group(1))
        elif m.group(2) is not None:
            out.append("*" if m.group(2) == "*" else int(m.group(2)))
        else:
            out.append(m.group(3))
    return out


def select(doc: Any, path: str) -> list[Any]:
    """Every value the path selects (a list, possibly empty)."""
    cur = [doc]
    for tok in parse_path(path):
        nxt: list[Any] = []
        for node in cur:
            if tok == "*":
                if isinstance(node, list):
                    nxt.extend(node)
                elif isinstance(node, dict):
                    nxt.extend(node.values())
            elif isinstance(tok, int):
                if isinstance(node, list) and -len(node) <= tok < len(node):
                    nxt.append(node[tok])
            elif isinstance(node, dict) and tok in node:
                nxt.append(node[tok])
        cur = nxt
    return cur


def first(doc: Any, path: str) -> Any:
    got = select(doc, path)
    return got[0] if got else None
