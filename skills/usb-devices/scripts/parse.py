"""Parse the attached USB devices and resolve them through usb.ids (a lookup tool)."""
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

LK = None


def _lk(cid, tool, args):
    """A lookup answer: the tool's data, {} when its table is missing, None while it is still being fetched."""
    d = LK.call(cid, tool, args)
    return None if d is None else ({} if d.get("_failed") else d)


def _hex4(text):
    if not text:
        return None
    if "apple_vendor_id" in text:
        return "05ac"
    m = re.search(r"0x([0-9a-fA-F]{1,4})", text)
    return m.group(1).lower().zfill(4) if m else None


def _walk(items, depth=0):
    for it in items:
        yield it, depth
        yield from _walk(it.get("_items", []), depth + 1)


def _parse(captures, platform):
    c = by_name(captures)
    devices = []
    src = ""
    if platform == "macos":
        d = json_of(c.get("sp_usb"))
        if d:
            src = "sp_usb"
            for it, depth in _walk(d.get("SPUSBDataType", [])):
                vid = _hex4(it.get("vendor_id"))
                if not vid:
                    continue  # a bus, not a device
                devices.append({"vid": vid, "pid": _hex4(it.get("product_id")) or "0000", "name": it.get("_name"), "serial": it.get("serial_num"),
                                "location": it.get("location_id"), "speed": it.get("device_speed"), "mfr": it.get("manufacturer")})
        elif c.get("ioreg_usb") and c["ioreg_usb"].ok:
            src = "ioreg_usb"
            for blk in re.split(r"\+-o ", c["ioreg_usb"].text)[1:]:
                v = re.search(r'"idVendor" = (\d+)', blk)
                p = re.search(r'"idProduct" = (\d+)', blk)
                if v and p:
                    n = re.search(r'"USB Product Name" = "([^"]*)"', blk) or re.match(r"([^@<\n]+)", blk)
                    devices.append({"vid": f"{int(v.group(1)):04x}", "pid": f"{int(p.group(1)):04x}", "name": n.group(1).strip() if n else None,
                                    "serial": (re.search(r'"USB Serial Number" = "([^"]*)"', blk) or [None, None])[1]})
        else:
            return lost(c.get("sp_usb"), "usb.device_count")
    else:
        lu = c.get("lsusb")
        if lu is None or not lu.ok:
            return lost(lu, "usb.device_count")
        src = "lsusb"
        for ln in lu.text.splitlines():
            m = re.match(r"Bus (\d+) Device (\d+): ID ([0-9a-f]{4}):([0-9a-f]{4})\s*(.*)$", ln)
            if m and m.group(3) != "1d6b":  # 1d6b is the Linux Foundation root hub
                devices.append({"vid": m.group(3), "pid": m.group(4), "name": m.group(5).strip() or None, "serial": None, "location": f"{m.group(1)}-{m.group(2)}"})
    out = [FactOut("usb.device_count", len(devices), src=src)]
    check = _lk("usb_table", "lookup_usb_id", {"vid": "0000"})  # is the usb.ids table there at all?
    if check == {}:
        out.append(UnknownOut("usb.ids_table", Reason.unavailable, "run `envrec data fetch` to download the real usb.ids"))
    for dv in devices:
        d = _lk(f"usb_{dv['vid']}_{dv['pid']}", "lookup_usb_id", {"vid": dv["vid"], "pid": dv["pid"]}) or {}
        vendor, product = d.get("vendor"), d.get("product")
        ident = dv["serial"] or f"{dv['vid']}:{dv['pid']}:{dv.get('location') or ''}"
        attrs = {"vid": dv["vid"], "pid": dv["pid"], "name": dv.get("name") or product, "vendor": vendor, "product": product, "speed": dv.get("speed")}
        resolved = bool(vendor and (product or dv.get("name")))
        out.append(Entity("usb", 1, ident, "owned", attrs, resolved=resolved, src=src, confidence=0.9 if product else 0.7))
    return out


def _run(captures, platform, caps):
    global LK
    LK = Lookups(caps)
    return _parse(captures, platform)


def parse(inp):
    return run_parser(inp, _run)
