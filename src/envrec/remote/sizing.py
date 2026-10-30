"""The size table moved to `envrec.sizing` (the MAS commands must not import this retired package). Everything is re-exported so the older importers keep working."""

from envrec.sizing import CATALOG_PARAMS_B, RAM_TABLE, ModelChoice, choose_model, disk_needed_gb, ram_cap_params_b

__all__ = ["CATALOG_PARAMS_B", "RAM_TABLE", "ModelChoice", "choose_model", "disk_needed_gb", "ram_cap_params_b"]
