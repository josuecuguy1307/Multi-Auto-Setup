"""A scan for credential-shaped strings in anything that is stored (facts, events, logs, files the setup wrote). It looks at the SHAPE of well-known credentials and at `name: value` pairs where the name says
secret; it never reads a credential from anywhere, and a hit reports the kind and a masked sample, never the value. It is a net for a leak that should not happen (the design is that no secret is ever read), so
a hit is a failure of that design, not a thing to filter."""

from __future__ import annotations

import json
import re
from typing import Any

PATTERNS: dict[str, re.Pattern[str]] = {
    "api key (sk-)": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "hugging face token": re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
    "aws access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "google api key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    "slack token": re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "bearer credential": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    "secret-named value": re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|token)[\"']?\s*[:=]\s*[\"']?[^\s\"',}\]]{6,}"),
}


SECRET_FIELDS = re.compile(r"(?i)^(password|passwd|secret|client_secret|api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|private[_-]?key)$")


def _walk(obj: Any, out: list[dict[str, str]]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if SECRET_FIELDS.match(str(k)) and isinstance(v, str) and len(v) >= 6:
                out.append({"kind": "secret-named field", "sample": f"{k}=…({len(v)} chars)"})
            _walk(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk(v, out)
    elif isinstance(obj, str):
        for kind, pat in PATTERNS.items():
            for m in pat.finditer(obj):
                out.append({"kind": kind, "sample": m.group(0)[:6] + "…" + f"({len(m.group(0))} chars)"})


def scan(obj: Any) -> list[dict[str, str]]:
    """Every credential-shaped string in `obj` (text, or nested dicts and lists, JSON-looking text included). Returns [{kind, sample}] with the sample masked."""
    out: list[dict[str, str]] = []
    _walk(obj, out)
    if isinstance(obj, str) and obj.lstrip().startswith(("{", "[")):
        try:
            _walk(json.loads(obj), out)
        except ValueError:
            pass
    return out
