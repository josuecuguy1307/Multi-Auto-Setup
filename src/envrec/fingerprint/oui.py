"""IEEE OUI list: the organization a MAC prefix was assigned to. Identifies the maker of the interface chip, not the device."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from envrec.fingerprint.base import Candidate, Loader, content_version, hex_digits

_LENGTHS = {"MA-L": 6, "MA-M": 7, "MA-S": 9}


class IeeeOui(Loader):
    source = "ieee.oui"

    def __init__(self, assignments: dict[str, str], data_version: str) -> None:
        self._assignments, self.data_version = assignments, data_version

    @classmethod
    def from_text(cls, text: str) -> IeeeOui:
        table: dict[str, str] = {}
        for row in csv.DictReader(io.StringIO(text)):
            code, name = (row.get("Assignment") or "").strip().lower(), (row.get("Organization Name") or "").strip()
            if code and name and len(code) == _LENGTHS.get((row.get("Registry") or "").strip(), len(code)):
                table[code] = name
        return cls(table, content_version(text.encode()))

    @classmethod
    def from_file(cls, path: Path | str) -> IeeeOui:
        return cls.from_text(Path(path).read_text(encoding="utf-8", errors="replace"))

    def lookup(self, identifier: str) -> list[Candidate]:
        """A MAC address or any prefix of at least 6 hex digits. The longest assignment wins."""
        digits = hex_digits(identifier)
        for n in (9, 7, 6):  # MA-S, MA-M, MA-L
            if len(digits) >= n and (name := self._assignments.get(digits[:n])):
                return [Candidate(self.source, digits[:n], f"{digits[:n].upper()}  {name}", self.data_version, "prefix")]
        return []
