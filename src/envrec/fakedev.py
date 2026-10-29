"""A fake serial device on a pseudo-terminal, for tests and for the proof runs. The harness opens its slave path exactly as it would open a real port.

The device speaks from a spec: lines it emits on its own (cycled, one every `period_s`), and canned replies to the few fixed queries. It records every byte written to
it, so a test can prove that nothing outside the fixed table was ever sent. It is never real hardware."""

from __future__ import annotations

import json
import os
import pty
import select
import threading
import time
import tty
from pathlib import Path


class PtyDevice:
    def __init__(self, stream: list[str] | None = None, responses: dict[str, str] | None = None, *, period_s: float = 0.3, name: str = "fake") -> None:
        self.name, self.stream, self.responses, self.period_s = name, list(stream or []), dict(responses or {}), period_s
        self.master, self.slave = pty.openpty()
        tty.setraw(self.master)
        self.port = os.ttyname(self.slave)
        self.received = b""
        self._stop = False
        self._i = 0
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    @classmethod
    def from_replay(cls, path: Path | str, period_s: float = 0.3, steady: bool = True) -> PtyDevice:
        """A replay fixture as a live device. `steady` repeats the first frame (a sensor in a steady room), so every capture reads the same value; otherwise the whole transcript cycles."""
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        stream = d.get("stream", [])[:1] if steady else d.get("stream", [])
        return cls(stream, d.get("responses", {}), period_s=period_s, name=d.get("name", "fake"))

    def _loop(self) -> None:
        last = 0.0
        while not self._stop:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                try:
                    data = os.read(self.master, 256)
                except OSError:
                    break
                self.received += data
                for q, reply in self.responses.items():
                    if data.endswith(q.encode("latin-1")) and reply:
                        os.write(self.master, reply.encode("latin-1"))
            now = time.monotonic()
            if self.stream and now - last > self.period_s:
                last = now
                try:
                    os.write(self.master, self.stream[self._i % len(self.stream)].encode("latin-1"))
                except OSError:
                    break
                self._i += 1

    def close(self) -> None:
        self._stop = True
        self._t.join(timeout=1)
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> PtyDevice:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
