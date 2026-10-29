"""Parse the Bluetooth controller and the devices paired with this machine."""
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


def _company(vid):
    if not vid:
        return None
    d = _lk("btc_" + re.sub(r"[^a-z0-9]+", "_", str(vid).lower()), "lookup_bluetooth_company", {"company_id": str(vid)})
    return (d or {}).get("name")


def _parse(captures, platform):
    c = by_name(captures)
    out: list = []
    if platform == "macos":
        d = json_of(c.get("sp_bluetooth"))
        if not d:
            return lost(c.get("sp_bluetooth"), *('ble.controller_state', 'ble.paired_count'))
        blk = d["SPBluetoothDataType"][0]
        ctl = blk.get("controller_properties") or {}
        out.append(FactOut("ble.controller_state", "on" if ctl.get("controller_state") == "attrib_on" else "off", src="sp_bluetooth"))
        if ctl.get("controller_chipset"):
            out.append(FactOut("ble.controller_chipset", ctl["controller_chipset"], src="sp_bluetooth"))
        devs = []
        for state, key in (("connected", "device_connected"), ("paired", "device_not_connected")):
            for item in blk.get(key, []):
                for name, props in item.items():
                    devs.append((name, state, props))
        out.append(FactOut("ble.paired_count", len(devs), src="sp_bluetooth"))
        out.append(FactOut("ble.connected_count", sum(1 for _, s, _ in devs if s == "connected"), src="sp_bluetooth"))
        for name, state, p in devs:
            vid = p.get("device_vendorID")
            company = _company(vid)
            battery = p.get("device_batteryLevelMain") or p.get("device_batteryLevel")
            attrs = {"name": name, "type": p.get("device_minorType"), "state": state, "vendor": company, "battery": battery}
            out.append(Entity("bluetooth", 1, p.get("device_address") or name, "owned", attrs, resolved=bool(p.get("device_minorType") or company),
                              prefix="ble", src="sp_bluetooth"))
        return out
    show = c.get("bt_show")
    if show is None or not show.ok:
        return lost(show, *('ble.controller_state', 'ble.paired_count'))
    out.append(FactOut("ble.controller_state", "on" if re.search(r"Powered: yes", show.text) else "off", src="bt_show"))
    paired = [ln for ln in (c["bt_paired"].text.splitlines() if c.get("bt_paired") and c["bt_paired"].ok else []) if ln.startswith("Device ")]
    connected = {ln.split()[1] for ln in (c["bt_connected"].text.splitlines() if c.get("bt_connected") and c["bt_connected"].ok else []) if ln.startswith("Device ")}
    out += [FactOut("ble.paired_count", len(paired), src="bt_paired"), FactOut("ble.connected_count", len(connected), src="bt_connected")]
    for ln in paired:
        parts = ln.split(maxsplit=2)
        out.append(Entity("bluetooth", 1, parts[1], "owned", {"name": parts[2] if len(parts) > 2 else None, "state": "connected" if parts[1] in connected else "paired"},
                          resolved=len(parts) > 2, prefix="ble", src="bt_paired"))
    return out


def _run(captures, platform, caps):
    global LK
    LK = Lookups(caps)
    return _parse(captures, platform)


def parse(inp):
    return run_parser(inp, _run)
