"""Small deterministic reference tables installed for parity tests and golden capture (the real tables are large and absent in CI)."""

from pathlib import Path

from envrec.discovery import tables
from envrec.fingerprint import BluetoothCompanies, BluetoothServices, IeeeOui, MdnsServiceTypes, UsbIds

ROOT = Path(__file__).resolve().parent.parent

IANA_CSV = """Service Name,Port Number,Transport Protocol,Description
ipp,631,tcp,IPP (Internet Printing Protocol)
http,80,tcp,World Wide Web HTTP
ssh,22,tcp,The Secure Shell (SSH) Protocol
smb,445,tcp,SMB file sharing
spotify-connect,,tcp,Spotify Connect player
matter,5540,tcp,Matter Operational Discovery and Communi
matterd,5550,udp,Matter Commissioner Discovery
"""


def install() -> None:
    tables.override("usb_ids", UsbIds.from_file(ROOT / "fixtures" / "samples" / "usb_ids_sample.txt"))
    tables.override("oui", IeeeOui({"30f947": "Router Maker Ltd", "2824c9": "Amazon Technologies Inc.", "c45bac": "Apple, Inc.", "ccb11a": "Samsung Electronics Co.,Ltd",
                                    "ac416a": "Amazon Technologies Inc.", "b0739c": "Amazon Technologies Inc.", "00005e": "IANA Documentation"}, "golden"))
    tables.override("bt_companies", BluetoothCompanies({76: "Apple, Inc.", 117: "Samsung Electronics Co. Ltd.", 93: "Harman International"}, "golden"))
    tables.override("bt_services", BluetoothServices({"180f": "Battery Service", "1800": "Generic Access"}, "golden"))
    tables.override("iana", MdnsServiceTypes.from_text(IANA_CSV))


def uninstall() -> None:
    for n in ("usb_ids", "oui", "bt_companies", "bt_services", "iana"):
        tables.override(n, None)
