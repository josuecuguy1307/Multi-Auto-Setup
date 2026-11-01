"""FIXTURE (violates B3): a director validator that accepts a route or a machine from the model."""
from envrec_board.director import SPEC


def validate_action(action, view):
    args = action.get("args", {})
    return action["action"], {k: v for k, v in args.items() if k in SPEC[action["action"]] or k in ("route", "machine", "node_id", "model")}
