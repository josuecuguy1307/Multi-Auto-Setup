"""Fixed fact-key namespace. A key is `<prefix>.<segment>[.<segment>...]`."""

from __future__ import annotations

import re

NAMESPACE: frozenset[str] = frozenset(
    {
        "host", "os", "hw", "usb", "serial", "ble", "net", "instr",
        "signal", "app", "fw", "repo", "sensor", "ring", "skill", "ios", "media", "spotify", "route", "workspace", "door",
    }
)

_SEGMENT = re.compile(r"^[a-z0-9_\-]+$")

# Which key prefix a target kind writes its identity facts under.
KIND_PREFIX = {
    "host": "host", "usb": "usb", "serial": "serial", "ble": "ble", "net_host": "net",
    "instrument": "instr", "signal": "signal", "app": "app", "repo": "repo",
    "sensor": "sensor", "phone": "ios", "remote": "host", "door": "door", "media": "media", "scope": "host", "unknown": "host",
}


def check_key(key: str) -> str:
    parts = key.split(".")
    if len(parts) < 2 or not all(_SEGMENT.match(p) for p in parts):
        raise ValueError(f"fact key {key!r} must look like 'prefix.name' with lowercase segments")
    if parts[0] not in NAMESPACE:
        raise ValueError(
            f"fact key {key!r} has unknown prefix {parts[0]!r}; allowed: {sorted(NAMESPACE)}"
        )
    return key
