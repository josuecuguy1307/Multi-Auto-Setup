from pathlib import Path

from envrec.fingerprint.base import Candidate, Loader, default_data_dir
from envrec.fingerprint.bluetooth import BluetoothCompanies, BluetoothServices
from envrec.fingerprint.facts import serial_protocol_result, single_lookup_result, usb_lookup_result
from envrec.fingerprint.mdns import MdnsServiceTypes
from envrec.fingerprint.oui import IeeeOui
from envrec.fingerprint.serial_signatures import SerialMatch, match_serial
from envrec.fingerprint.usb_ids import UsbIds

_FILES = {
    "usb": ("usb.ids", UsbIds),
    "oui": ("oui.csv", IeeeOui),
    "ble_company": ("bt_company_ids.json", BluetoothCompanies),
    "ble_service": ("bt_service_uuids.json", BluetoothServices),
    "mdns": ("iana_services.csv", MdnsServiceTypes),
}


def load_available(data_dir: Path | str | None = None) -> dict[str, Loader]:
    """Loaders for whichever downloaded files exist. Missing files are simply absent."""
    root = Path(data_dir) if data_dir else default_data_dir()
    return {name: cls.from_file(root / fname) for name, (fname, cls) in _FILES.items() if (root / fname).is_file()}


__all__ = [
    "BluetoothCompanies", "BluetoothServices", "Candidate", "IeeeOui", "Loader", "MdnsServiceTypes",
    "SerialMatch", "UsbIds", "load_available", "match_serial", "serial_protocol_result",
    "single_lookup_result", "usb_lookup_result",
]
