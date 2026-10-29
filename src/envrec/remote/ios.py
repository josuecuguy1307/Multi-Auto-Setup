"""A phone on a cable, read-only, through libimobiledevice. Fixed table: nothing outside it can be asked.

The table (which keys can be read, how each becomes a fact, the error rules and the plan sentence) is DATA in the `connect-ios` skill's references/lockdown-table.json: edit that file,
not this module. Policy lives here and in the runner, in code. The device is named in targets.yaml by the SHA-256 of its UDID; the UDID itself is read from `idevice_id -l` only to pick
the attached device whose hash matches and is passed to the next process, never stored, logged or returned. The device name is kept as a salted hash. The owner's Trust tap is theirs:
nothing here pairs, retries around a refusal, or touches the passcode.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from envrec.skills.data import reference_json

_T = reference_json("connect-ios", "lockdown-table.json")

# aspect -> [(fact key, lockdown domain or None, key)]
ASPECTS: dict[str, list[tuple[str, str | None, str]]] = {aspect: [(f, d, k) for f, d, k, _how in rows] for aspect, rows in _T["aspects"].items()}
CONVERT: dict[str, str] = {f: how for rows in _T["aspects"].values() for f, _d, _k, how in rows}  # fact key -> text | int | bool | gb | hash
HIDDEN_VALUE = set(_T["hidden_keys"])  # read, hashed by the caller, never shown or kept in clear

_UDID = re.compile(_T["udid_pattern"])

HINT = _T["hint"]


def udid_hash(udid: str) -> str:
    return hashlib.sha256(udid.strip().lower().encode()).hexdigest()


def parse_udids(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if _UDID.match(ln.strip())]


def classify(code: int, out: str, err: str) -> tuple[str, str]:
    """(category, plain sentence) for a failed libimobiledevice call. The category is what the caller branches on."""
    text = f"{out}\n{err}".lower()
    for rule in _T["errors"]:
        if ("code" in rule and code == rule["code"]) or ("regex" in rule and re.search(rule["regex"], text)):
            return rule["category"], rule["sentence"]
    return _T["failed_category"], (err.strip().splitlines() or [_T["failed_default"]])[-1][:140]


@dataclass
class IosRead:
    ok: bool
    category: str = "ok"
    detail: str = ""
    values: dict[str, str] | None = None  # lockdown key -> value as printed (DeviceName is already replaced by its marker)
    raw: str = ""  # what the capture holds: KEY=value lines, the name only as <hidden>


def plan_text(aspect: str, device_hint: str | None = None) -> str:
    keys = ", ".join(k for _, _, k in ASPECTS[aspect])
    return _T["plan_template"].format(aspect=aspect, device_hint=device_hint or _T["default_device_hint"], keys=keys)
