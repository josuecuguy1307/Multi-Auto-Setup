"""Windows: restrict the access control list to the current user. UNTESTED: written without a Windows machine; the first real run needs a Windows machine."""

from __future__ import annotations

import getpass
import subprocess
from pathlib import Path


class WindowsLayer:
    family = "windows"

    def protect_private(self, path: Path) -> None:
        user = getpass.getuser()
        done = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F" if Path(path).is_dir() else f"{user}:F"], capture_output=True, text=True, timeout=30)
        if done.returncode != 0:
            raise OSError(f"could not restrict {path.name} to the current user: {(done.stderr or done.stdout).strip()[:160]}")

    def is_private(self, path: Path) -> bool:
        done = subprocess.run(["icacls", str(path)], capture_output=True, text=True, timeout=30)
        others = [ln for ln in done.stdout.splitlines() if ("Everyone" in ln or "BUILTIN\\Users" in ln or "Authenticated Users" in ln)]
        return done.returncode == 0 and not others
