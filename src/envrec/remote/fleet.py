"""Fleet inventory: a read-only capability table across enrolled devices, from preflight alone."""

from __future__ import annotations

from dataclasses import dataclass

from envrec.mission.models import Transport
from envrec.remote.preflight import Preflight, PreflightError
from envrec.remote.provision import Provisioner


@dataclass
class FleetRow:
    target_id: str
    ok: bool
    note: str = ""
    preflight: Preflight | None = None
    model_params_b: float = 0.0
    limited_by: str = ""


def inventory_fleet(p: Provisioner, ids_and_transports: dict[str, Transport]) -> list[FleetRow]:
    rows: list[FleetRow] = []
    for tid, transport in ids_and_transports.items():
        try:
            pf = p.check(tid, transport)  # observe-tier templates only; writes facts, touches nothing on the device
        except (PreflightError, Exception) as exc:  # one broken device must not hide the others
            rows.append(FleetRow(tid, False, f"{type(exc).__name__}: {str(exc)[:120]}"))
            continue
        choice = p.select_model(tid, pf)
        rows.append(FleetRow(tid, True, "", pf, choice.params_b, choice.limited_by))
    return rows


def render_table(rows: list[FleetRow]) -> str:
    head = "| device | OS | arch | RAM (effective) | free disk | pkg mgr | python | largest model |"
    lines = [head, "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if not r.ok:
            lines.append(f"| {r.target_id} | unreachable: {r.note} | | | | | | |")
            continue
        pf = r.preflight
        lines.append(f"| {r.target_id} | {pf.os_id or pf.os_name} | {pf.arch} | {pf.ram_effective_gib} GiB | {pf.disk_free_gb} GB | "
                     f"{pf.pkg_manager} | {'yes' if pf.python else 'no'} | "
                     f"{(str(r.model_params_b) + 'B') if r.model_params_b else 'none'} ({r.limited_by}) |")
    return "\n".join(lines)
