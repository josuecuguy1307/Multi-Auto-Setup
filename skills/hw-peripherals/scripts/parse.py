"""Parse cameras, audio devices and printers by name."""
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

def strip_prefix(value, prefix):
    return value[len(prefix):] if isinstance(value, str) and value.startswith(prefix) else value


def _parse(captures, platform):
    c = by_name(captures)
    out: list = []
    if platform != "macos":
        dev = c.get("dev_video")
        if dev is None or not dev.ok:
            return lost(dev, "hw.camera_count")
        n = [x for x in dev.text.split() if re.fullmatch(r"video\d+", x)]
        return [FactOut("hw.camera_count", len(n), src="dev_video")] + [Entity("camera", 0, x, "owned", {"name": x}, src="dev_video") for x in n]
    cam = json_of(c.get("sp_camera"))
    if cam:
        items = cam["SPCameraDataType"]
        out.append(FactOut("hw.camera_count", len(items), src="sp_camera"))
        out += [Entity("camera", 0, str(i.get("spcamera_unique-id") or i["_name"]), "owned", {"name": i.get("_name")}, src="sp_camera") for i in items]
    else:
        out += lost(c.get("sp_camera"), "hw.camera_count")
    aud = json_of(c.get("sp_audio"))
    if aud:
        devs = [i for blk in aud["SPAudioDataType"] for i in blk.get("_items", [])]
        out.append(FactOut("hw.audio_device_count", len(devs), src="sp_audio"))
        for i in devs:
            out.append(Entity("audio", 0, i.get("_name", "audio"), "owned",
                              {"name": i.get("_name"), "input": bool(i.get("coreaudio_device_input")), "output": bool(i.get("coreaudio_device_output")),
                               "transport": strip_prefix(i.get("coreaudio_device_transport"), "coreaudio_device_type_")}, src="sp_audio"))
    else:
        out += lost(c.get("sp_audio"), "hw.audio_device_count")
    pr = json_of(c.get("sp_printers"))
    if pr:
        items = pr.get("SPPrintersDataType", [])
        out.append(FactOut("hw.printer_count", len(items), src="sp_printers"))
        out += [Entity("printer", 0, i.get("_name", "printer"), "owned", {"name": i.get("_name"), "status": i.get("status"), "uri_scheme": str(i.get("uri", "")).split(":")[0] or None}, src="sp_printers") for i in items]
    elif c.get("sp_printers") is not None and c["sp_printers"].ok:
        out.append(FactOut("hw.printer_count", 0, src="sp_printers"))
    else:
        out += lost(c.get("sp_printers"), "hw.printer_count")
    return out


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))
