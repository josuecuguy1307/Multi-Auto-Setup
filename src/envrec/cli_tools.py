"""`envrec tools list|show`: what the harness can do, straight from the registry."""

from __future__ import annotations

import sys

from envrec.tools import all_tools, get_tool


def cmd_tools(args) -> int:
    if args.tools_cmd == "list":
        rows = [t for t in all_tools() if (not args.tag or args.tag in t.tags)]
        print(f"{'TOOL':26} {'TIER':8} {'PLATFORMS':16} {'TAGS':34} SUMMARY")
        for t in rows:
            print(f"{t.name:26} {t.tier:8} {','.join(t.platforms):16} {','.join(t.tags)[:34]:34} {t.summary}")
        print(f"\n{len(rows)} tools ({sum(1 for t in rows if t.tier == 'read')} read-tier: they ask before they send anything)")
        return 0
    t = get_tool(args.name)
    if t is None:
        print(f"no tool named {args.name!r}", file=sys.stderr)
        return 2
    print(f"{t.signature_text()}  [{t.tier}, {','.join(t.platforms)}, provenance {t.provenance}]\n{t.summary}")
    for a in t.args:
        print(f"  {a.name} ({a.type_name}{'' if a.required else ', optional'}): {a.doc}")
    print(f"Returns: {t.returns}\nNotes: {t.notes}\nTags: {', '.join(t.tags)}")
    return 0


def add_tools_commands(sub) -> None:
    p = sub.add_parser("tools", help="the generic tools the harness can call")
    ts = p.add_subparsers(dest="tools_cmd", required=True)
    ls = ts.add_parser("list")
    ls.add_argument("--tag", default=None)
    sh = ts.add_parser("show")
    sh.add_argument("name")
    p.set_defaults(func=cmd_tools)
