"""Bluetooth assigned numbers: company ids and service UUIDs (via the Nordic compilation of SIG data)."""

from __future__ import annotations

import json
from pathlib import Path

from envrec.fingerprint.base import Candidate, Loader, content_version, hex_digits

_SIG_BASE_SUFFIX = "-0000-1000-8000-00805f9b34fb"


class BluetoothCompanies(Loader):
    source = "bluetooth.company_ids"

    def __init__(self, names: dict[int, str], data_version: str) -> None:
        self._names, self.data_version = names, data_version

    @classmethod
    def from_text(cls, text: str) -> BluetoothCompanies:
        return cls({int(e["code"]): e["name"] for e in json.loads(text)}, content_version(text.encode()))

    @classmethod
    def from_file(cls, path: Path | str) -> BluetoothCompanies:
        return cls.from_text(Path(path).read_text(encoding="utf-8"))

    def lookup(self, identifier: str) -> list[Candidate]:
        """Company id as hex ('004c', '0x004C')."""
        digits = hex_digits(identifier)
        if not digits:
            return []
        code = int(digits, 16)
        name = self._names.get(code)
        return [Candidate(self.source, f"0x{code:04x}", f"0x{code:04X}  {name}", self.data_version, "company")] if name else []


class BluetoothServices(Loader):
    source = "bluetooth.service_uuids"

    def __init__(self, names: dict[str, str], data_version: str) -> None:
        self._names, self.data_version = names, data_version

    @classmethod
    def from_text(cls, text: str) -> BluetoothServices:
        return cls({e["uuid"].lower(): e["name"] for e in json.loads(text)}, content_version(text.encode()))

    @classmethod
    def from_file(cls, path: Path | str) -> BluetoothServices:
        return cls.from_text(Path(path).read_text(encoding="utf-8"))

    def lookup(self, identifier: str) -> list[Candidate]:
        """16-bit ('180f', '0x180F') or the full 128-bit form on the SIG base UUID."""
        text = identifier.strip().lower()
        if len(text) == 36 and text.endswith(_SIG_BASE_SUFFIX):
            text = text[4:8] if text.startswith("0000") else text[:8]
        digits = hex_digits(text)
        name = self._names.get(digits)
        return [Candidate(self.source, digits, f"{digits}  {name}", self.data_version, "service")] if name else []
