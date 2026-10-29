"""Reference tables for discovery, loaded once from data/downloads (fetched by `envrec data fetch`). A missing file means None: the adapter
then reports the lookup as unavailable instead of guessing. Tests inject small tables through `override`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from envrec.discovery.commands import data_dir
from envrec.fingerprint import BluetoothCompanies, BluetoothServices, IeeeOui, MdnsServiceTypes, UsbIds

_FILES = {"usb_ids": ("usb.ids", UsbIds), "oui": ("oui.csv", IeeeOui), "bt_companies": ("bt_company_ids.json", BluetoothCompanies),
          "bt_services": ("bt_service_uuids.json", BluetoothServices), "iana": ("iana_services.csv", MdnsServiceTypes)}
_OVERRIDE: dict[str, object] = {}


def override(name: str, table: object | None) -> None:
    if table is None:
        _OVERRIDE.pop(name, None)
    else:
        _OVERRIDE[name] = table


@lru_cache(maxsize=8)
def _load(name: str, directory: str):
    fname, cls = _FILES[name]
    p = Path(directory) / fname
    return cls.from_file(p) if p.is_file() else None


def get(name: str):
    return _OVERRIDE.get(name) or _load(name, str(data_dir() / "downloads"))


def usb_ids() -> UsbIds | None:
    return get("usb_ids")


def oui() -> IeeeOui | None:
    return get("oui")


def bt_companies() -> BluetoothCompanies | None:
    return get("bt_companies")


def bt_services() -> BluetoothServices | None:
    return get("bt_services")


def iana() -> MdnsServiceTypes | None:
    return get("iana")


def name_of(table, identifier: str) -> str | None:
    """The human name behind a lookup, or None when the table has nothing (or is missing)."""
    if table is None:
        return None
    c = table.lookup(identifier)
    return c[0].entry.split("  ", 1)[-1].split(": ", 1)[-1] if c else None
