"""A translating adapter: it calls the core and reports what the core said."""
import json


def call_tool(core, name, args):
    out = core.call("POST", "decisions", {"intent_id": args["intent_id"], "option": args["option"]})
    return json.dumps(out)
