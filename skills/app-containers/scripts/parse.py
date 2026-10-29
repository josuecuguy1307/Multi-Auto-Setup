"""Parse the running containers and virtual machines."""
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
    n = 0
    asked = []
    for name in ("docker_ps", "podman_ps"):
        cap = c.get(name)
        if cap and cap.ok:
            asked.append(name)
            for ln in cap.text.splitlines():
                try:
                    j = json.loads(ln)
                except ValueError:
                    continue
                n += 1
                out.append(Entity("container", 0, f"{name}:{j.get('ID') or j.get('Id')}", "owned", {"runtime": name.split("_")[0], "image": j.get("Image"), "state": j.get("State") or j.get("Status"),
                                                                                                 "name": (j.get("Names") if isinstance(j.get("Names"), str) else (j.get("Names") or [""])[0])}, src=name))
    for name, rt in (("prlctl", "parallels"), ("vbox", "virtualbox"), ("utm", "utm")):
        cap = c.get(name)
        if cap and cap.ok:
            asked.append(name)
            lines = [ln for ln in cap.text.splitlines() if ln.strip()][1:] if name == "utm" else [ln for ln in cap.text.splitlines() if ln.strip()]
            for ln in lines:
                n += 1
                out.append(Entity("container", 0, f"{name}:{ln}", "owned", {"runtime": rt, "name": ln.split('"')[1] if '"' in ln else ln[:60]}, src=name))
    if not asked:
        return [UnknownOut("app.container_count", Reason.not_applicable, "no container or VM tool answered")]
    return [FactOut("app.container_count", n, src=asked[0]), FactOut("app.container_runtimes", asked, src=asked[0])] + out


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))
