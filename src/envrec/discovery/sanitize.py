"""Everything that comes from the world is untrusted text. Clean it, cap it, and render it only as quoted data."""

from __future__ import annotations

import re
import secrets
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f  ​-‏‪-‮⁦-⁩]")
DEFAULT_CAP = 80


def clean_text(value: object, cap: int = DEFAULT_CAP) -> str:
    """Strip ANSI and control characters, collapse whitespace, cap the length. Always returns a plain string."""
    s = _ANSI.sub("", str(value))
    s = _CTRL.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= cap else s[: cap - 1] + "…"


_ID_PATTERNS = (re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\b"),
                re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"), re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{12,16}(?![0-9A-Fa-f])"))


def mask_ids(value: object, cap: int = DEFAULT_CAP) -> str:
    """clean_text plus: device identifiers hiding in names (MAC-like hex runs, UUIDs) become <id>, so a MAC never reaches the model through an mDNS name."""
    s = clean_text(value, cap * 2)
    for p in _ID_PATTERNS:
        s = p.sub("<id>", s)
    return clean_text(s, cap)


def quoted(value: object, cap: int = DEFAULT_CAP) -> str:
    """How a world string appears in the model's view: always inside quotes, so it reads as data and never as an instruction."""
    s = clean_text(value, cap).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def clean_attrs(attrs: dict, cap: int = DEFAULT_CAP) -> dict:
    out: dict = {}
    for k, v in attrs.items():
        key = re.sub(r"[^a-z0-9_]+", "_", str(k).lower()).strip("_")[:32] or "attr"
        if isinstance(v, bool | int | float) or v is None:
            out[key] = v
        elif isinstance(v, list | tuple):
            out[key] = [clean_text(x, cap) if isinstance(x, str) else x for x in list(v)[:20]]
        else:
            out[key] = clean_text(v, cap)
    return out


class Salts:
    """`stable` makes ids of OWNED things the same on every run of this machine; `run` is random per run, so an ambient
    device (a stranger's BLE advertiser) cannot be followed from one run to the next."""

    def __init__(self, stable_file: Path | None = None) -> None:
        self.run = secrets.token_hex(16)
        self._stable_file = stable_file
        self._stable: str | None = None

    @property
    def stable(self) -> str:
        if self._stable is None:
            f = self._stable_file
            if f is not None and f.is_file():
                self._stable = f.read_text().strip()
            else:
                self._stable = secrets.token_hex(16)
                if f is not None:
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_text(self._stable)
        return self._stable
