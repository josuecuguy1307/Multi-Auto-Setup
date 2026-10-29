"""The per-OS layer: the ONLY place that touches operating-system specifics (permissions, access control lists, which family we run on).

Everything else (coordinator, node agent, channel) is OS-independent and asks this layer through the typed interface below. What is *known* about an operating system (where it keeps
a program's state, say) is not written here: it is data in a skill (`skills/os-state-paths/references/state-dirs.json`) that this layer reads. Conformance P1 and H1 keep it that way.
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import sys
from pathlib import Path
from typing import Protocol

SKILLS_DIR = Path(os.environ.get("MAS_SKILLS_DIR") or Path(__file__).resolve().parents[2] / "skills")
STATE_PATHS_FILE = SKILLS_DIR / "os-state-paths" / "references" / "state-dirs.json"


class OsLayer(Protocol):
    family: str  # darwin | linux | windows

    def protect_private(self, path: Path) -> None:
        """Make a file or folder readable and writable by the current user only."""

    def is_private(self, path: Path) -> bool:
        """True when only the current user can read the file or folder."""


def family() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def current() -> OsLayer:
    if family() == "windows":
        from envrec_os.windows import WindowsLayer
        return WindowsLayer()
    from envrec_os.posix import PosixLayer
    return PosixLayer(family())


def _expand(template: str, env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    s = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}", lambda m: env.get(m.group(1)) or m.group(2), template)
    s = re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", lambda m: env.get(m.group(1), m.group(0)), s)
    return Path(os.path.expanduser(s))


def default_state_dir(env: dict[str, str] | None = None) -> Path:
    """`MAS_STATE_DIR`, else the folder the os-state-paths skill gives for this family. A family without a row has no default."""
    env = os.environ if env is None else env
    if env.get("MAS_STATE_DIR"):
        return Path(env["MAS_STATE_DIR"]).expanduser()
    table = json.loads(STATE_PATHS_FILE.read_text())
    template = table.get(family())
    if not template:
        raise LookupError(f"no default state folder is known for the {family()} family: pass --state-dir (add a row to {STATE_PATHS_FILE.name} to give it one)")
    return _expand(template, env)


def card() -> dict:
    """The node card basics a machine can report about itself: host name, operating system family and release, CPU architecture. Facts, not hardware detail."""
    return {"hostname": socket.gethostname(), "os": family(), "os_release": platform.release(), "arch": platform.machine().lower()}
