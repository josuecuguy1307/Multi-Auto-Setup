"""macOS and Linux: owner-only permission bits."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class PosixLayer:
    def __init__(self, family: str) -> None:
        self.family = family

    def protect_private(self, path: Path) -> None:
        os.chmod(path, 0o700 if Path(path).is_dir() else 0o600)

    def is_private(self, path: Path) -> bool:
        return (os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO)) == 0
