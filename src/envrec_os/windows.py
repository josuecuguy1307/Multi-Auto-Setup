"""Windows: restrict the access control list to the current user. UNTESTED: written without a Windows machine; the first real run needs a Windows machine (spawn_detached and make_executable included)."""

from __future__ import annotations

import getpass
import os
import subprocess
from contextlib import contextmanager
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

    def make_executable(self, path: Path) -> None:
        return None  # Windows has no execute bit: a file runs by its extension

    @contextmanager
    def raw_terminal(self, fd: int):
        yield  # UNTESTED: the console already delivers one key at a time

    def read_key(self, stream) -> str:
        import msvcrt  # UNTESTED: written without a Windows machine
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": "\x1b[A", "P": "\x1b[B"}.get(msvcrt.getwch(), "\x1b")
        return ch

    def enable_ansi(self) -> None:
        """Turn on virtual terminal processing for stdout and UTF-8 output, so the banner's colors and blocks show. UNTESTED here (needs Windows)."""
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        kernel32.SetConsoleOutputCP(65001)

    def spawn_detached(self, argv: list[str], cwd: Path, log_path: Path, env: dict[str, str] | None = None) -> int:
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW: no console, not killed with the node's
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags, close_fds=True, env={**os.environ, **env} if env else None)
        return proc.pid
