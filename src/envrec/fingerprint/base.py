"""Shared shape for every public-database loader: identifier in, candidate entries out."""

from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Candidate:
    source: str  # e.g. "usb.ids"
    identifier: str  # what was looked up, normalized
    entry: str  # the list entry as text
    data_version: str  # version from the file header, or a content hash
    level: str = ""  # e.g. "vendor", "product"


class Loader(ABC):
    source: str
    data_version: str

    @abstractmethod
    def lookup(self, identifier: str) -> list[Candidate]:
        """Candidates for an identifier. Empty list when the list has nothing."""


def content_version(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()[:12]


def default_data_dir() -> Path:
    return Path(os.environ.get("ENVREC_DATA_DIR", Path.cwd() / "data" / "downloads"))


def hex_digits(text: str) -> str:
    """Lowercase hex digits only: '00:1A-2b.3C' -> '001a2b3c'."""
    return "".join(c for c in text.lower().removeprefix("0x") if c in "0123456789abcdef")
