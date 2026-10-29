"""Parse battery and thermal state."""
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

def _parse(captures, platform):
    c = by_name(captures)
    out: list = []
    if platform == "macos":
        d = json_of(c.get("sp_power"))
        if d:
            for blk in d["SPPowerDataType"]:
                ch = blk.get("sppower_battery_charge_info")
                if ch:
                    out += [FactOut("hw.battery_percent", ch.get("sppower_battery_state_of_charge"), src="sp_power"),
                            FactOut("hw.battery_charging", ch.get("sppower_battery_is_charging") == "TRUE", src="sp_power")]
                    hl = blk.get("sppower_battery_health_info") or {}
                    out += [FactOut("hw.battery_cycle_count", hl.get("sppower_battery_cycle_count"), src="sp_power"),
                            FactOut("hw.battery_health", hl.get("sppower_battery_health"), src="sp_power")]
        else:
            out += lost(c.get("sp_power"), "hw.battery_percent")
        t = c.get("pmset_therm")
        if t and t.ok:
            warn = "No thermal warning" not in t.text
            out.append(FactOut("hw.thermal_state", "warning" if warn else "nominal", src="pmset_therm"))
        return [o for o in out if not isinstance(o, FactOut) or o.value is not None]
    cap, st = c.get("bat_capacity"), c.get("bat_status")
    if cap and cap.ok:
        out += [FactOut("hw.battery_percent", int(cap.text.strip()), src="bat_capacity")]
        if st and st.ok:
            out.append(FactOut("hw.battery_charging", st.text.strip().lower() == "charging", src="bat_status"))
        return out
    return [UnknownOut("hw.battery_percent", Reason.not_applicable, "no battery exposed")]


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))
