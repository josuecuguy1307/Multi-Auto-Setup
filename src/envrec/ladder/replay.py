"""Replay devices: recorded transcripts that stand in for a USB-serial gadget. Never real hardware.

A replay device has a USB descriptor, an unsolicited stream (read one chunk at a time) and canned
replies to a short list of safe identification commands. Anything else gets silence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# The only things the ladder ever sends. Read-only identification queries.
SAFE_COMMANDS = ("*IDN?\r\n", "AT\r\n", "$I\r\n")


@dataclass
class ReplayDevice:
    name: str
    handle: str
    kind: str
    descriptor: dict
    stream: list[str]
    responses: dict[str, str]
    synthetic: bool = True
    _cursor: int = field(default=0, repr=False)

    def passive_read(self) -> str:
        """The next unsolicited chunk, or '' when the device has nothing more to say."""
        if self._cursor < len(self.stream):
            chunk = self.stream[self._cursor]
            self._cursor += 1
            return chunk
        return ""

    def send(self, command: str) -> str:
        if command not in SAFE_COMMANDS:
            raise ValueError(f"refusing to send {command!r}: not an allowlisted identification query")
        return self.responses.get(command, "")

    def fresh(self) -> ReplayDevice:
        return ReplayDevice(self.name, self.handle, self.kind, self.descriptor, list(self.stream),
                            dict(self.responses), self.synthetic)


class DeviceRegistry:
    def __init__(self, devices: list[ReplayDevice] | None = None) -> None:
        self._by_handle = {d.handle: d for d in (devices or [])}

    def get(self, handle: str) -> ReplayDevice | None:
        return self._by_handle.get(handle)

    def handles(self) -> list[str]:
        return sorted(self._by_handle)


def load_device(path: Path | str) -> ReplayDevice:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return ReplayDevice(data["name"], data["handle"], data["kind"], data["descriptor"], data["stream"],
                        data.get("responses", {}), data.get("synthetic", True))


def load_devices(directory: Path | str) -> DeviceRegistry:
    return DeviceRegistry([load_device(p) for p in sorted(Path(directory).glob("*.json"))])
