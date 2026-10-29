"""Parse the current Wi-Fi link and counts of nearby networks by channel."""
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

SETTINGS_LOCATION = ("System Settings > Privacy & Security > Location Services: turn it on, and enable it for the program that runs this "
                     "(Terminal, or the app hosting your shell). macOS hides the Wi-Fi network name without it.")


SETTINGS_LOCATION = ("System Settings > Privacy & Security > Location Services: turn it on, and enable it for the program that runs this "
                     "(Terminal, or the app hosting your shell). macOS hides the Wi-Fi network name without it.")

def _parse(captures, platform):
    c = captures[0]
    if platform != "macos":
        if not c.ok:
            return lost(c, "net.wifi.connected")
        rows = [ln.split(":") for ln in c.text.splitlines() if ln]
        cur = [r for r in rows if r and r[0] == "yes"]
        by_chan: dict[str, int] = {}
        for r in rows:
            if len(r) > 2 and r[0] != "yes":
                by_chan[r[2]] = by_chan.get(r[2], 0) + 1
        out = [FactOut("net.wifi.connected", bool(cur), src="nmcli_active"), FactOut("net.wifi.nearby_count", sum(by_chan.values()), src="nmcli_active"),
               FactOut("net.wifi.nearby_by_channel", by_chan, src="nmcli_active", limitations=["counts only; nearby names are never stored"])]
        if cur and len(cur[0]) >= 4:
            out += [FactOut("net.wifi.channel", cur[0][2], src="nmcli_active"), FactOut("net.wifi.rssi", int(cur[0][3]), src="nmcli_active")]
        return out
    d = json_of(c)
    if not d:
        return lost(c, "net.wifi.connected")
    out: list = []
    ifs = d["SPAirPortDataType"][0].get("spairport_airport_interfaces", [])
    wifi = next((i for i in ifs if i.get("spairport_current_network_information") and i.get("spairport_wireless_card_type")), None)
    if wifi is None:
        return [FactOut("net.wifi.connected", False, src="sp_airport", limitations=["no Wi-Fi interface reported"])]
    cur = wifi["spairport_current_network_information"]
    connected = wifi.get("spairport_status_information") == "spairport_status_connected"
    out.append(FactOut("net.wifi.connected", connected, src="sp_airport"))
    if connected:
        name = cur.get("_name")
        if name in (None, "<redacted>"):
            out.append(UnknownOut("net.wifi.ssid", Reason.permission, SETTINGS_LOCATION))
        else:
            out.append(FactOut("net.wifi.ssid", name, src="sp_airport"))
        ch = str(cur.get("spairport_network_channel", ""))
        m = re.match(r"(\d+) \((\d+)GHz", ch)
        if m:
            out += [FactOut("net.wifi.channel", int(m.group(1)), src="sp_airport"), FactOut("net.wifi.band_ghz", int(m.group(2)), src="sp_airport")]
        sn = re.match(r"(-?\d+) dBm / (-?\d+) dBm", str(cur.get("spairport_signal_noise", "")))
        if sn:
            out += [FactOut("net.wifi.rssi", int(sn.group(1)), src="sp_airport"), FactOut("net.wifi.noise", int(sn.group(2)), src="sp_airport")]
        out += [FactOut("net.wifi.phy", cur.get("spairport_network_phymode"), src="sp_airport"),
                FactOut("net.wifi.security", str(cur.get("spairport_security_mode", "")).replace("spairport_security_mode_", ""), src="sp_airport"),
                FactOut("net.wifi.tx_rate", cur.get("spairport_network_rate"), src="sp_airport")]
    others = wifi.get("spairport_airport_other_local_wireless_networks") or []
    by_chan: dict[str, int] = {}
    for n in others:
        m = re.match(r"(\d+)", str(n.get("spairport_network_channel", "")))
        if m:
            by_chan[m.group(1)] = by_chan.get(m.group(1), 0) + 1
    out += [FactOut("net.wifi.nearby_count", len(others), src="sp_airport", limitations=["counts only; nearby network names are never stored"]),
            FactOut("net.wifi.nearby_by_channel", by_chan, src="sp_airport")]
    return [o for o in out if not isinstance(o, FactOut) or o.value is not None]


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))
