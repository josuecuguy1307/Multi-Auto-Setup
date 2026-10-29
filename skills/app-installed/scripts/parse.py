"""Parse the installed applications and packages."""
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
        d = json_of(c.get("sp_apps"))
        apps: list[dict] = []
        if d:
            for a in d["SPApplicationsDataType"]:
                apps.append({"name": a.get("_name"), "version": a.get("version"), "from": a.get("obtained_from"), "arch": str(a.get("arch_kind", "")).replace("arch_", "")})
            src = "sp_apps"
        elif c.get("ls_apps") and c["ls_apps"].ok:
            apps = [{"name": n[:-4]} for n in c["ls_apps"].text.splitlines() if n.endswith(".app")]
            src = "ls_apps"
        else:
            return lost(c.get("sp_apps"), "app.installed_count")
        kinds = Counter(a.get("from") or "unknown" for a in apps)
        out.append(FactOut("app.installed_count", len(apps), src=src))
        out.append(FactOut("app.installed_by_source", dict(kinds), src=src))
        out += [Entity("app", 0, "app:" + str(a["name"]), "owned", {k: v for k, v in a.items() if v}, src=src) for a in apps]
        b = c.get("brew")
        if b and b.ok:
            pk = [ln.split()[0] for ln in b.text.splitlines() if ln.strip()]
            out.append(FactOut("app.brew_formulae", {"count": len(pk), "names": pk[:150]}, src="brew"))
        return out
    pkgs = 0
    for name, parser in (("dpkg", lambda t: [ln for ln in t.splitlines() if ln.startswith("ii")]), ("rpm", lambda t: t.splitlines()),
                         ("pacman", lambda t: t.splitlines()), ("flatpak", lambda t: t.splitlines()), ("snap", lambda t: t.splitlines()[1:])):
        cap = c.get(name)
        if cap and cap.ok:
            items = parser(cap.text)
            out.append(FactOut(f"app.packages.{name}", len(items), src=name))
            pkgs += len(items)
    if pkgs == 0 and not out:
        return [UnknownOut("app.installed_count", Reason.not_applicable, "no supported package manager answered")]
    return [FactOut("app.installed_count", pkgs)] + out


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))
