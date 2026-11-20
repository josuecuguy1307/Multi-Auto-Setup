"""The setup plan a coordinator offers and a node checks before asking anybody for anything (D-036).

    {"v": 1, "fleet": <name>, "coordinator": <name>, "steps": [{"title": <text>, "files": {<path>: <base64>}}, ...]}

A node writes only under `.mas/` in the folder its owner chose; every path segment is plain; nothing that names a credential store is
accepted (the gate would refuse it anyway, D-025); at most MAX_STEPS steps, MAX_FILES files a step and MAX_PLAN_BYTES of content. Each step
becomes one gate action, so one press. The plan's hash binds the JOIN_FLEET approval to exactly this plan.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re

from mas_device import approval as ap
from mas_device.gated import is_credential_path

MAX_STEPS = 8
MAX_FILES = 500
MAX_PLAN_BYTES = 4 << 20
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_SEG = re.compile(r"[A-Za-z0-9._-]{1,100}")
_TITLE = re.compile(r"[ -~]{1,80}")
ROOT_DIR = ".mas"


class PlanRefused(Exception):
    pass


def check_path(path) -> str:
    if not isinstance(path, str):
        raise PlanRefused("a path is text")
    parts = path.split("/")
    if len(parts) < 2 or parts[0] != ROOT_DIR:
        raise PlanRefused(f"{path!r} is not under {ROOT_DIR}/")
    for p in parts[1:]:
        if p in (".", "..") or not _SEG.fullmatch(p):
            raise PlanRefused(f"{path!r} has a segment that is not plain")
    if is_credential_path(path):
        raise PlanRefused(f"{path!r} names a credential store")
    return path


def validate(plan) -> list[dict[str, bytes]]:
    """The decoded steps, or PlanRefused."""
    if not isinstance(plan, dict) or set(plan) != {"v", "fleet", "coordinator", "steps"}:
        raise PlanRefused("a plan is exactly {v, fleet, coordinator, steps}")
    if type(plan["v"]) is not int or plan["v"] != 1:
        raise PlanRefused("plan version 1 only")
    for k in ("fleet", "coordinator"):
        if not isinstance(plan[k], str) or not NAME.fullmatch(plan[k]):
            raise PlanRefused(f"{k} is a plain name")
    steps = plan["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise PlanRefused(f"1 to {MAX_STEPS} steps")
    out, total = [], 0
    for step in steps:
        if not isinstance(step, dict) or set(step) != {"title", "files"}:
            raise PlanRefused("a step is exactly {title, files}")
        if not isinstance(step["title"], str) or not _TITLE.fullmatch(step["title"]):
            raise PlanRefused("a step title is short printable text")
        files = step["files"]
        if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
            raise PlanRefused(f"a step writes 1 to {MAX_FILES} files")
        decoded = {}
        for path, b64 in files.items():
            check_path(path)
            if not isinstance(b64, str):
                raise PlanRefused("file content is base64 text")
            try:
                data = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError):
                raise PlanRefused(f"{path!r}: content is not base64") from None
            if base64.b64encode(data).decode("ascii") != b64:
                raise PlanRefused(f"{path!r}: content is not canonical base64")
            total += len(data)
            if total > MAX_PLAN_BYTES:
                raise PlanRefused(f"the plan writes more than {MAX_PLAN_BYTES} bytes")
            decoded[path] = data
        out.append(decoded)
    return out


def plan_hash(plan: dict) -> str:
    return hashlib.sha256(b"MAS-FLEET-PLAN-v1\0" + ap.canonical(plan)).hexdigest()
