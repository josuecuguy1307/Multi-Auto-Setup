"""macOS and Linux: owner-only permission bits."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


class PosixLayer:
    def __init__(self, family: str) -> None:
        self.family = family

    def protect_private(self, path: Path) -> None:
        os.chmod(path, 0o700 if Path(path).is_dir() else 0o600)

    def is_private(self, path: Path) -> bool:
        return (os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO)) == 0

    def make_executable(self, path: Path) -> None:
        os.chmod(path, (os.stat(path).st_mode & 0o7777) | stat.S_IXUSR)

    def enable_ansi(self) -> None:
        """Terminals here already show escape codes and UTF-8."""

    def spawn_detached(self, argv: list[str], cwd: Path, log_path: Path, env: dict[str, str] | None = None) -> int:
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True, env={**os.environ, **env} if env else None)  # its own session: it does not die with the node's terminal
        return proc.pid
