"""macOS and Linux: owner-only permission bits."""

from __future__ import annotations

import os
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path


class PosixLayer:
    def __init__(self, family: str) -> None:
        self.family = family
        self._pending = bytearray()  # a byte read past an Esc that starts the next key

    def protect_private(self, path: Path) -> None:
        os.chmod(path, 0o700 if Path(path).is_dir() else 0o600)

    def is_private(self, path: Path) -> bool:
        return (os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO)) == 0

    def make_executable(self, path: Path) -> None:
        os.chmod(path, (os.stat(path).st_mode & 0o7777) | stat.S_IXUSR)

    def enable_ansi(self) -> None:
        """Terminals here already show escape codes and UTF-8."""

    @contextmanager
    def raw_terminal(self, fd: int):
        import termios
        import tty
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            yield
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def read_key(self, stream) -> str:
        # Unbuffered fd reads only, one byte at a time: a buffered text read
        # slurps an arrow's bytes (the fd then looks empty and the arrow is
        # misreported as Esc), and a greedy multi-byte read after an Esc eats
        # the next queued key. A byte that does not belong to an arrow
        # sequence is kept in _pending for the next call.
        import select
        fd = stream.fileno()

        def take() -> bytes:
            if self._pending:
                return bytes([self._pending.pop(0)])
            return os.read(fd, 1)

        def queued() -> bool:
            return bool(self._pending) or bool(select.select([fd], [], [], 0.05)[0])

        first = take()
        if first != b"\x1b":
            return first.decode("utf-8", "replace")
        if not queued():
            return "\x1b"
        second = take()
        if second != b"[":
            self._pending += second  # the start of the next key, not part of this Esc
            return "\x1b"
        if not queued():
            return "\x1b["
        return "\x1b[" + take().decode("utf-8", "replace")

    def spawn_detached(self, argv: list[str], cwd: Path, log_path: Path, env: dict[str, str] | None = None) -> int:
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True, env={**os.environ, **env} if env else None)  # its own session: it does not die with the node's terminal
        return proc.pid
