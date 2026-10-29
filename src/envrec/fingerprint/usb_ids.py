"""usb.ids: USB vendor and product names by hex id."""

from __future__ import annotations

import re
from pathlib import Path

from envrec.fingerprint.base import Candidate, Loader, content_version, hex_digits

_VENDOR = re.compile(r"^([0-9a-f]{4})\s{2}(.+)$")
_PRODUCT = re.compile(r"^\t([0-9a-f]{4})\s{2}(.+)$")
_VERSION = re.compile(r"^#\s*Version:\s*(\S+)")


class UsbIds(Loader):
    source = "usb.ids"

    def __init__(self, vendors: dict[str, str], products: dict[tuple[str, str], str], data_version: str) -> None:
        self._vendors, self._products, self.data_version = vendors, products, data_version

    @classmethod
    def from_text(cls, text: str) -> UsbIds:
        vendors: dict[str, str] = {}
        products: dict[tuple[str, str], str] = {}
        version = content_version(text.encode())
        vendor: str | None = None
        for line in text.splitlines():
            if m := _VERSION.match(line):
                version = m.group(1)
                continue
            if not line or line.startswith("#"):
                continue
            if m := _VENDOR.match(line):
                vendor = m.group(1)
                vendors[vendor] = m.group(2).strip()
            elif m := _PRODUCT.match(line):
                if vendor is not None:
                    products[(vendor, m.group(1))] = m.group(2).strip()
            elif not line.startswith("\t"):
                vendor = None  # a class/language section starts: its tabbed lines are not products
        return cls(vendors, products, version)

    @classmethod
    def from_file(cls, path: Path | str) -> UsbIds:
        return cls.from_text(Path(path).read_text(encoding="utf-8", errors="replace"))

    def vendor(self, vid: str) -> str | None:
        return self._vendors.get(hex_digits(vid).zfill(4))

    def product(self, vid: str, pid: str) -> str | None:
        return self._products.get((hex_digits(vid).zfill(4), hex_digits(pid).zfill(4)))

    def lookup(self, identifier: str) -> list[Candidate]:
        """'1a86' finds the vendor; '1a86:7523' finds the vendor and, if listed, the product."""
        vid, _, pid = identifier.strip().partition(":")
        v = hex_digits(vid).zfill(4)
        out: list[Candidate] = []
        if name := self._vendors.get(v):
            out.append(Candidate(self.source, v, f"{v}  {name}", self.data_version, "vendor"))
            if pid:
                p = hex_digits(pid).zfill(4)
                if pname := self._products.get((v, p)):
                    out.append(Candidate(self.source, f"{v}:{p}", f"{v}:{p}  {name} / {pname}",
                                         self.data_version, "product"))
        return out
