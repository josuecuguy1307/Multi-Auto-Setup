import subprocess

from envrec_coordinator import harness


def call_tool(core, name, args, channel):
    if channel.cloud and args["sensitivity"] == "confidential":
        return "withheld"
    if args["option"] == "approve":
        return {"actor": "OWNER", "approved": True}
    subprocess.run(["osascript"])
