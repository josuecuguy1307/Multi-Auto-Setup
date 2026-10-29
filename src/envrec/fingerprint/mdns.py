"""mDNS service types, from the IANA service-name registry (where `_name._tcp` names are registered)."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from envrec.fingerprint.base import Candidate, Loader, content_version

_TYPE = re.compile(r"^_?([a-z0-9][a-z0-9\-]*)\._?(tcp|udp)(\.local)?\.?$", re.I)


class MdnsServiceTypes(Loader):
    source = "iana.service_names"

    def __init__(self, entries: dict[tuple[str, str], list[str]], data_version: str) -> None:
        self._entries, self.data_version = entries, data_version

    @classmethod
    def from_text(cls, text: str) -> MdnsServiceTypes:
        table: dict[tuple[str, str], list[str]] = {}
        for row in csv.DictReader(io.StringIO(text)):
            name = (row.get("Service Name") or "").strip().lower()
            transport = (row.get("Transport Protocol") or "").strip().lower()
            desc = " ".join((row.get("Description") or "").split())
            if name and transport and desc and desc not in table.setdefault((name, transport), []):
                table[(name, transport)].append(desc)
        return cls(table, content_version(text.encode()))

    @classmethod
    def from_file(cls, path: Path | str) -> MdnsServiceTypes:
        return cls.from_text(Path(path).read_text(encoding="utf-8", errors="replace"))

    def lookup(self, identifier: str) -> list[Candidate]:
        """A service type such as '_ipp._tcp' or '_ipp._tcp.local.'."""
        m = _TYPE.match(identifier.strip())
        if not m:
            return []
        name, transport = m.group(1).lower(), m.group(2).lower()
        return [
            Candidate(self.source, f"_{name}._{transport}", f"{name} ({transport}): {desc}", self.data_version, "service")
            for desc in self._entries.get((name, transport), [])
        ]
