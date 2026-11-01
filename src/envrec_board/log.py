"""The board's append-only log. There is no update and no delete: the only calls are `append` and `entries`. The board's state is always rebuilt from it (`Board.replay`).

`MemoryLog` is for tests; `JsonlLog` keeps one JSON line per entry under the state dir. Seam: the coordinator's own hash-chained `Store` can implement the same two calls later."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Protocol

from envrec_board.vocab import Entry


class BoardLog(Protocol):
    def append(self, entry: Entry) -> Entry: ...
    def entries(self) -> list[Entry]: ...


class MemoryLog:
    def __init__(self) -> None:
        self._items: list[Entry] = []
        self._lock = threading.Lock()

    def append(self, entry: Entry) -> Entry:
        with self._lock:
            entry.n = len(self._items) + 1
            self._items.append(entry)
            return entry

    def entries(self) -> list[Entry]:
        with self._lock:
            return list(self._items)


class JsonlLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._count = len(self._read())

    def _read(self) -> list[Entry]:
        if not self.path.exists():
            return []
        return [Entry.from_dict(json.loads(line)) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def append(self, entry: Entry) -> Entry:
        with self._lock:
            self._count += 1
            entry.n = self._count
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
            return entry

    def entries(self) -> list[Entry]:
        with self._lock:
            return self._read()
