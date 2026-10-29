"""Parse the storage volumes."""
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
        d = json_of(c.get("sp_storage"))
        if not d:
            return lost(c.get("sp_storage"), "hw.volume_count")
        seen = set()
        for v in d["SPStorageDataType"]:
            mount = v.get("mount_point", "")
            phys = v.get("physical_drive") or {}
            if v.get("_name") in seen and mount.startswith("/System/Volumes/Update"):
                continue
            seen.add(v.get("_name"))
            out.append(Entity("volume", 0, str(v.get("volume_uuid") or v.get("_name")), "owned",
                              {"name": v.get("_name"), "fs": v.get("file_system"), "size_gib": round((v.get("size_in_bytes") or 0) / GIB, 1),
                               "free_gib": round((v.get("free_space_in_bytes") or 0) / GIB, 1), "internal": phys.get("is_internal_disk") == "yes",
                               "medium": phys.get("medium_type"), "mount": mount if mount in ("/", "/System/Volumes/Data") or mount.startswith("/Volumes") else None},
                              src="sp_storage"))
        ext = [e for e in out if not e.attrs["internal"]]
        return [FactOut("hw.volume_count", len(out), src="sp_storage"), FactOut("hw.external_volume_count", len(ext), src="sp_storage")] + out
    lb = json_of(c.get("lsblk"))
    if not lb:
        return lost(c.get("lsblk"), "hw.volume_count")
    for dev in lb.get("blockdevices", []):
        if dev.get("type") in ("disk", "part") and not str(dev.get("name", "")).startswith(("loop", "ram")):
            out.append(Entity("volume", 0, dev["name"], "owned", {"name": dev["name"], "type": dev.get("type"), "fs": dev.get("fstype"),
                                                                   "size_gib": round(int(dev.get("size") or 0) / GIB, 1), "internal": not dev.get("rm"),
                                                                   "model": (dev.get("model") or "").strip() or None, "mount": dev.get("mountpoint")}, src="lsblk"))
    return [FactOut("hw.volume_count", len(out), src="lsblk")] + out


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))
