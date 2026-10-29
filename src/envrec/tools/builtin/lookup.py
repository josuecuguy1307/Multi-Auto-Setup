"""Reference-table lookups. The tables are downloaded data (`envrec data fetch`); a missing table is reported, never guessed around."""

from __future__ import annotations

from envrec.discovery import tables
from envrec.discovery.types import Reason
from envrec.tools.ctx import ToolContext, ToolResult
from envrec.tools.registry import tool


def _answer(table, name: str, identifier: str, source: str) -> ToolResult:
    if table is None:
        return ToolResult.fail(Reason.unavailable, f"the {source} table is not downloaded: run `envrec data fetch`")
    cands = table.lookup(identifier)
    return ToolResult(data={"found": bool(cands), "name": tables.name_of(table, identifier), "source": source, "identifier": identifier,
                            "entries": [c.entry for c in cands][:5]}, provider=source)


@tool(tags=("table_lookup", "identify"))
def lookup_usb_id(ctx: ToolContext, vid: str, pid: str = "") -> ToolResult:
    """Look up a USB vendor and product id in usb.ids.

    Args:
        vid: the 4-digit hexadecimal vendor id.
        pid: the 4-digit hexadecimal product id, or empty to look up the vendor only.
    Returns:
        data.found, data.vendor, data.product (when the list has them), data.name and data.entries from the public usb.ids list.
    Tier: observe
    Platforms: any
    Notes: a vendor match names the vendor, not the model.
    """
    table = tables.usb_ids()
    res = _answer(table, "usb", f"{vid}:{pid}" if pid else vid, "usb.ids")
    if res.ok:
        res.data["vendor"] = table.vendor(vid)
        res.data["product"] = table.product(vid, pid) if pid else None
    return res


@tool(tags=("table_lookup", "identify"))
def lookup_oui(ctx: ToolContext, mac_prefix: str) -> ToolResult:
    """Look up the vendor behind a network hardware address prefix.

    Args:
        mac_prefix: a MAC address or its first six hexadecimal digits.
    Returns:
        data.found and data.name from the IEEE OUI list.
    Tier: observe
    Platforms: any
    Notes: randomized (locally administered) addresses have no vendor.
    """
    return _answer(tables.oui(), "oui", mac_prefix, "ieee.oui")


@tool(tags=("table_lookup", "identify"))
def lookup_bluetooth_company(ctx: ToolContext, company_id: str) -> ToolResult:
    """Look up a Bluetooth company id.

    Args:
        company_id: the company id in hexadecimal, for example 0x004c.
    Returns:
        data.found and data.name from the Bluetooth SIG numbers.
    Tier: observe
    Platforms: any
    Notes: none
    """
    return _answer(tables.bt_companies(), "bt", company_id, "bluetooth.company_ids")


@tool(tags=("table_lookup", "identify"))
def lookup_bluetooth_service(ctx: ToolContext, uuid: str) -> ToolResult:
    """Look up a Bluetooth service UUID.

    Args:
        uuid: a 16-bit service id or the full 128-bit UUID.
    Returns:
        data.found and data.name from the Bluetooth SIG numbers.
    Tier: observe
    Platforms: any
    Notes: none
    """
    return _answer(tables.bt_services(), "bt", uuid, "bluetooth.service_uuids")


@tool(tags=("table_lookup", "identify"))
def lookup_service_type(ctx: ToolContext, service_type: str) -> ToolResult:
    """Look up what a network service type means in the IANA registry.

    Args:
        service_type: a service type such as _ipp._tcp.
    Returns:
        data.found and data.name from the IANA service-name registry.
    Tier: observe
    Platforms: any
    Notes: types the registry does not list are unknown, not guessed.
    """
    return _answer(tables.iana(), "iana", service_type, "iana.service_names")


@tool(tags=("table_lookup", "identify"))
def match_serial_signature(ctx: ToolContext, text: str, sent: str = "") -> ToolResult:
    """Match a serial reply against the known protocol signatures.

    Args:
        text: what the device said.
        sent: what was sent just before, if anything (a fixed query such as *IDN?).
    Returns:
        data.matches: a list of {protocol, confidence, detail}, best first.
    Tier: observe
    Platforms: any
    Notes: signatures cover NMEA, SCPI, AT, G-code and Modbus; a match is a hypothesis.
    """
    from envrec.fingerprint import match_serial
    ms = match_serial(text, sent.strip() or None) if text else []
    return ToolResult(data={"matches": [{"protocol": m.protocol, "confidence": m.confidence, "detail": m.evidence} for m in ms[:5]]}, provider="serial.signatures")
