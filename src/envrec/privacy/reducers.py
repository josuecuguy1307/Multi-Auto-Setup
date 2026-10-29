"""Every privacy reducer in one place: sanitize, mask identifiers, salted hashing, secret scrubbing, the MCP reduction and the TXT whitelist.

They are applied at the fact boundary by `envrec.privacy.gate`, never by individual tools or skills, so no skill can write around them."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any

from envrec.discovery.sanitize import Salts, clean_attrs, clean_text, mask_ids, quoted  # noqa: F401  (re-exported: this module is the one home of the reducers)

TXT_KEYS = ("model", "md", "ty", "product", "manufacturer", "mfg", "am")
# Keys whose VALUES must never be stored: arguments, environment, credentials. A dict key matching any of these is dropped wherever it appears.
DROP_KEYS = re.compile(r"(^|_)(env|environment|args|argv|cmdline|command_line|token|tokens|password|passwd|secret|api_?key|apikey|authorization|auth|cookie|cookies|headers|bearer|credential|credentials|private_key|passphrase)(_|$)", re.I)
_SECRET_VALUES = (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{10,}"), re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.I), re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
                  re.compile(r"\bAKIA[0-9A-Z]{16}\b"), re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), re.compile(r"\b(password|passwd|token|secret|api[_-]?key)\s*[:=]\s*\S+", re.I),
                  re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"))
_ID_KEYS = {"vid", "pid", "oui", "ip", "port", "version", "build", "arch", "mac_prefix", "address", "cidr", "host", "model", "name", "id"}  # documented, non-secret identifiers
MAX_VALUE_CHARS = 20_000


def scrub_secrets(s: str) -> str:
    for rx in _SECRET_VALUES:
        s = rx.sub("[redacted]", s)
    return s


def clean_string(s: str, cap: int = 120, mask: bool = True) -> str:
    """One string from the world: control characters stripped, secrets scrubbed, identifiers masked, length capped."""
    s = scrub_secrets(str(s))
    return mask_ids(s, cap) if mask else clean_text(s, cap)


def clean_value(v: Any, depth: int = 0, cap: int = 120, mask: bool = True) -> Any:
    """Deep-clean a fact or attribute value. Never returns raw env, args or credentials."""
    if depth > 6:
        return None
    if isinstance(v, str):
        return clean_string(v, cap, mask)
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            key = clean_text(str(k), 60)
            if DROP_KEYS.search(re.sub(r"[^A-Za-z0-9_]+", "_", key)):
                continue
            out[key] = clean_value(x, depth + 1, cap, mask)
        return out
    if isinstance(v, (list, tuple)):
        return [clean_value(x, depth + 1, cap, mask) for x in list(v)[:500]]
    return v


def hash_identifier(value: str, salt: str, kind: str = "") -> str:
    from envrec.discovery.types import short_hash
    return short_hash(value, salt=salt)


# ---- capture reducers (applied BEFORE bytes reach the raw store) ---------------------------------------------------------------

def reduce_mcp_servers(raw: bytes, key: str = "mcpServers") -> bytes:
    """{server name: command basename} only. Arguments, environment, headers, tokens and URLs in the file are dropped."""
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return b"{}"
    if isinstance(d, dict) and key not in d and d and all(isinstance(v, str) for v in d.values()):
        return json.dumps({str(k)[:60]: os.path.basename(v)[:60] for k, v in d.items()}, sort_keys=True).encode()  # already reduced
    servers = d.get(key) if isinstance(d, dict) else None
    out: dict[str, str] = {}
    if isinstance(servers, dict):
        for name, spec in servers.items():
            cmd = spec.get("command") if isinstance(spec, dict) else None
            out[str(name)[:60]] = os.path.basename(str(cmd))[:60] if cmd else ("(remote url)" if isinstance(spec, dict) and spec.get("url") else "(unknown)")
    return json.dumps(out, sort_keys=True).encode()


def reduce_txt_whitelist(raw: bytes) -> bytes:
    """Keep the service line and only the whitelisted TXT keys of a Bonjour resolve: device ids, names and addresses in TXT never reach the raw store."""
    keep = []
    for ln in raw.decode("utf-8", "replace").splitlines():
        if "can be reached at" in ln:
            keep.append(ln)
        elif "=" in ln:
            pairs = [f"{k}={v[:40]}" for k, v in re.findall(r"(\w+)=((?:\\.|[^ ])*)", ln) if k.lower() in TXT_KEYS]
            if pairs:
                keep.append(" " + " ".join(pairs))
    return "\n".join(keep).encode()


CAPTURE_REDUCERS: dict[str, Callable[[bytes], bytes]] = {"mcp_servers": reduce_mcp_servers, "txt_whitelist": reduce_txt_whitelist}


def reduce_capture_bytes(data: bytes, reducer: str | None) -> bytes:
    if reducer is None:
        return data
    try:
        return CAPTURE_REDUCERS[reducer](data)
    except KeyError:
        raise ValueError(f"unknown capture reducer {reducer!r}") from None
