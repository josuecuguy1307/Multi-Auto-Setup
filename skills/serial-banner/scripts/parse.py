"""The first line a serial device says, as facts a signature can match."""
import json
import re

from envrec_sbx import Caps, Out, clean_text


def parse(inp):
    caps = Caps(inp)
    out = Out(caps)
    c = caps.get("probe")
    if c is None or not c.ok:
        return out.lost(c, "serial.banner", "serial.responded").result()
    try:
        log = json.loads(c.text)
    except ValueError:
        return out.lost(c, "serial.banner", "serial.responded").result()
    text = log.get("passive") or log.get("reply") or ""
    line = next((ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()), "")
    out.fact("serial.responded", bool(text), src="probe")
    if line:
        out.fact("serial.banner", clean_text(line, 60), src="probe", quote=line[:40] if line.isascii() else None, limitations=["the first line only; the rest of the capture is not kept"])
    else:
        out.unknown("serial.banner", "unavailable", "the device said nothing, heard or asked")
    if log.get("baud"):
        out.fact("serial.baud", log["baud"], src="probe")
    return out.result()
