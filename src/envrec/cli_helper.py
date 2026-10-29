"""`envrec helper build|status|remove`: the macOS app that gives the harness its own identity for privacy prompts."""

from __future__ import annotations

import json
import sys


def cmd_helper(args) -> int:
    from envrec import helper
    if sys.platform != "darwin":
        print("envrec Helper is for macOS only; on Linux the direct probes are used.")
        return 0
    if args.helper_cmd == "build":
        def approve(text: str) -> bool:
            print(text)
            if not sys.stdin.isatty():
                print("  (no terminal to ask you: not approved)")
                return False
            return input("  approve? [y/N] ").strip().lower() == "y"
        try:
            app = helper.build(approve=approve)
        except helper.HelperError as exc:
            print(exc.detail, file=sys.stderr)
            return 3 if exc.kind == "missing" else 1
        print(f"built {app} (compiled locally with swiftc, ad-hoc signed). The first time it is used, macOS asks once: Bluetooth, Local Network, Location. Click Allow.")
        if helper.last_reset_skipped:
            print(f"The privacy reset was NOT run. If macOS does not ask for permission after a rebuild, run it yourself: {helper.RESET_COMMAND}")
        return 0
    if args.helper_cmd == "status":
        print(json.dumps(helper.status(), indent=1))
        return 0
    res = helper.remove()
    print(json.dumps(res, indent=1))
    return 0


def add_helper_commands(sub) -> None:
    p = sub.add_parser("helper", help="build, check or remove envrec Helper (macOS: the harness's own app for Bluetooth / Local Network / Location prompts)")
    hs = p.add_subparsers(dest="helper_cmd", required=True)
    hs.add_parser("build", help="compile it here with swiftc and ad-hoc sign it (nothing is downloaded)")
    hs.add_parser("status", help="is it built, signed, running")
    hs.add_parser("remove", help="delete the app and clear the privacy entries macOS holds for its own bundle id")
    p.set_defaults(func=cmd_helper)
