"""Minimal S-expression reader/writer for KiCad files. Atoms stay strings; quoted strings are kept as `Q` so they round-trip quoted."""

from __future__ import annotations


class Q(str):
    """A quoted string atom."""


def parse(text: str):
    stack, cur, i, n = [], [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "(":
            stack.append(cur)
            cur = []
            i += 1
        elif c == ")":
            done = cur
            cur = stack.pop()
            cur.append(done)
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while text[j] != '"':
                if text[j] == "\\":
                    nxt = text[j + 1]
                    buf.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, nxt))
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            cur.append(Q("".join(buf)))
            i = j + 1
        elif c.isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            cur.append(text[i:j])
            i = j
    assert not stack, "unbalanced"
    return cur[0] if len(cur) == 1 else cur


def _atom(a) -> str:
    if isinstance(a, Q):
        return '"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if isinstance(a, float):
        s = f"{a:.4f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s
    return str(a)


def dump(x, indent: int = 0) -> str:
    if not isinstance(x, list):
        return _atom(x)
    simple = all(not isinstance(e, list) for e in x)
    if simple:
        return "(" + " ".join(_atom(e) for e in x) + ")"
    pad = "\t" * (indent + 1)
    head = [e for e in x[:1]]
    out = "(" + " ".join(_atom(e) for e in head)
    for e in x[1:]:
        out += "\n" + pad + dump(e, indent + 1)
    return out + "\n" + "\t" * indent + ")"


def find(x, key):
    """Direct children lists whose head is `key`."""
    return [e for e in x if isinstance(e, list) and e and e[0] == key]


def first(x, key):
    r = find(x, key)
    return r[0] if r else None


def walk(x):
    if isinstance(x, list):
        yield x
        for e in x:
            yield from walk(e)
