"""Turn an argv into the one command string a remote shell receives, for the shell the recipe names. Quoting only: no knowledge of what the command does."""

from __future__ import annotations

import re
import shlex
import subprocess

SHELLS = ("sh", "cmd", "powershell")
_PS_SAFE = re.compile(r"^[A-Za-z0-9_@%+=:,./\\-]+$")


def quote(argv: list[str], shell: str) -> str:
    if shell == "sh":
        return shlex.join(argv)
    if shell == "cmd":
        return subprocess.list2cmdline(argv)
    if shell == "powershell":
        return " ".join(a if _PS_SAFE.match(a) else "'" + a.replace("'", "''") + "'" for a in argv)
    raise ValueError(f"shell must be one of {SHELLS}")
