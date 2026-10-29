"""Parse the OS build, kernel string, locale and boot time."""
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
        sv = c.get("sw_vers")
        if sv and sv.ok:
            kv = dict(re.findall(r"^(\w+):\s+(.*)$", sv.text, re.M))
            out.append(FactOut("os.build", kv.get("BuildVersion"), src="sw_vers"))
        else:
            out += lost(sv, "os.build")
        bt = c.get("boottime")
        m = re.search(r"sec = (\d+)", bt.text) if bt and bt.ok else None
        if m:
            # uptime is derived by the caller against its own clock; here we keep the boot epoch, which is stable
            out.append(FactOut("os.boot_epoch", int(m.group(1)), src="boottime", limitations=["seconds since 1970 at boot"]))
        else:
            out += lost(bt, "os.boot_epoch")
        lc = c.get("locale")
        if lc and lc.ok and lc.text.strip():
            out.append(FactOut("os.locale", lc.text.strip(), src="locale"))
    else:
        rel = c.get("os_release")
        if rel and rel.ok:
            kv = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', rel.text, re.M))
            out += [FactOut("os.distro", kv.get("ID"), src="os_release"), FactOut("os.build", kv.get("VERSION_ID") or kv.get("BUILD_ID"), src="os_release")]
        else:
            out += lost(rel, "os.distro")
        up = c.get("proc_uptime")
        if up and up.ok:
            out.append(FactOut("os.uptime_s", int(float(up.text.split()[0])), src="proc_uptime"))
    un = c.get("uname")
    if un and un.ok:
        out.append(FactOut("os.uname", un.text.strip(), src="uname"))
    return [o for o in out if not isinstance(o, FactOut) or o.value is not None]


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))
