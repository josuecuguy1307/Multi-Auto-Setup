"""The family of an operating system from `uname -s`; anything else is unknown, with the reason."""
from envrec_sbx import Caps, Out


def parse(inp):
    caps = Caps(inp)
    out = Out(caps)
    c = caps.get("uname")
    if c is None or not c.ok:
        return out.lost(c, "os.family").result()
    word = c.text.strip().splitlines()[0].strip().lower() if c.text.strip() else ""
    family = {"linux": "linux", "darwin": "macos"}.get(word)
    if family is None:
        out.unknown("os.family", "unavailable", "uname answered with a name this skill does not know")
    else:
        out.fact("os.family", family, src="uname", quote=c.text.strip().splitlines()[0][:20])
    return out.result()
