"""How the owner's commands (`mas pair`) reach the running coordinator WITHOUT a second listening socket: small files in the coordinator's private `control/` folder, polled by the
server. The code the owner is shown exists in one file for the moment between `mas pair` writing it and the server picking it up (then the file is deleted), and in the server's
memory for two minutes. The folder is owner-only."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path

POLL_S = 0.25


def control_dir(state: Path) -> Path:
    return Path(state) / "control"


def write_json(layer, path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    layer.protect_private(path.parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    layer.protect_private(tmp)
    os.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


class ControlWatcher:
    """Runs inside the coordinator: picks up pairing offers, asks the owner through files, and reports each outcome back through a file."""

    def __init__(self, state: Path, layer, on_offer: Callable[[str, str], None], on_task: Callable[[str, str, str], None] | None = None) -> None:
        self.dir, self.layer, self.on_offer, self.on_task = control_dir(state), layer, on_offer, on_task

    async def run(self) -> None:
        while True:
            self.poll()
            await asyncio.sleep(POLL_S)

    def poll(self) -> None:
        if self.on_task is not None:
            for tf in sorted(self.dir.glob("task-req-*.json")):  # the owner asked for a task at the terminal (`mas task run`)
                req = read_json(tf)
                try:
                    tf.unlink()
                except OSError:
                    pass
                if req and isinstance(req.get("id"), str) and isinstance(req.get("node"), str) and isinstance(req.get("type"), str):
                    self.on_task(req["id"][:24], req["node"][:80], req["type"][:40])
        f = self.dir / "pair-offer.json"
        if not f.is_file():
            return
        req = read_json(f)
        try:
            f.unlink()  # the code leaves the disk the moment it is read
        except OSError:
            pass
        if not req or not isinstance(req.get("id"), str) or not str(req.get("code", "")).isdigit() or len(str(req["code"])) != 6:
            return
        write_json(self.layer, self.dir / f"offer-{req['id']}.json", {"status": "waiting"})
        node = req.get("node")
        if isinstance(node, str) and node:
            self.on_offer(req["id"], str(req["code"]), node)  # a code for one waiting machine
        else:
            self.on_offer(req["id"], str(req["code"]))

    async def ask_owner(self, summary: dict) -> bool:
        oid = summary["offer"]
        write_json(self.layer, self.dir / f"confirm-{oid}.json", {k: v for k, v in summary.items() if k != "offer"})
        decision = self.dir / f"decision-{oid}.json"
        try:
            while True:
                d = read_json(decision)
                if d is not None:
                    return d.get("accept") is True
                await asyncio.sleep(POLL_S)
        finally:
            for name in (f"confirm-{oid}.json", f"decision-{oid}.json"):
                try:
                    (self.dir / name).unlink()
                except OSError:
                    pass

    def report(self, offer_id: str, status: str, reason: str) -> None:
        write_json(self.layer, self.dir / f"offer-{offer_id}.json", {"status": status, "reason": reason})
