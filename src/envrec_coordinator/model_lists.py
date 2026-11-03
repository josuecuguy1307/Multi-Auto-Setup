"""The models an API lists, as a dated capture kept per environment (data: the environment's `models_list` in the catalog). The list is read through the node that holds the credential (the coordinator holds
no key), stored whole by its hash, and read again here by code: the ids the data's pattern accepts as chat models are the ones the router may offer for that environment, the declared default first. Nothing
here names a model: a model appears because the API's own list says so."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from envrec_coordinator.research import jget


class ModelLists:
    def __init__(self, dir: Path) -> None:
        self.dir = Path(dir)

    def _file(self, env_id: str) -> Path:
        return self.dir / f"{env_id}.json"

    def load(self, env_id: str) -> dict | None:
        try:
            doc = json.loads(self._file(env_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) and isinstance(doc.get("ids"), list) else None

    def save(self, entry: dict, access_id: str, machine: str, result: dict, *, clock=time.time) -> dict:
        """Store the node's answer (kept whole by its hash) and what code reads from it. Raises ValueError when the answer is not the agreed shape or its hash does not match."""
        from envrec_net import envrun as E
        ml = E.adapter_of(entry, access_id)["models_list"]
        body = str(result.get("body", ""))
        if hashlib.sha256(body.encode()).hexdigest() != result.get("sha256"):
            raise ValueError("the list does not match its hash")
        rows = jget(json.loads(body), ml["rows"])
        if not isinstance(rows, list):
            raise ValueError("the answer has no list of models")
        ids = [str(r[ml["id_field"]]) for r in rows if isinstance(r, dict) and isinstance(r.get(ml["id_field"]), str)]
        chat = [i for i in ids if re.fullmatch(ml["chat_pattern"], i)]
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "captures").mkdir(exist_ok=True)
        (self.dir / "captures" / f"{result['sha256']}.json").write_text(body, encoding="utf-8")
        doc: dict[str, Any] = {"environment": entry["id"], "access": access_id, "machine": machine, "url": result.get("url"), "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(clock())) + "Z",
                               "sha256": result["sha256"], "bytes": result.get("bytes"), "ids": ids, "chat_ids": chat}
        tmp = self._file(entry["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        tmp.replace(self._file(entry["id"]))
        return doc

    def selectable(self, entry: dict, access_id: str) -> list[str] | None:
        """The chat models to offer for this environment and access: the declared default first, then the others of the latest list; None when the entry has no such list or none was read yet."""
        from envrec_net import envrun as E
        if not E.adapter_of(entry, access_id).get("models_list"):
            return None
        doc = self.load(entry["id"])
        if doc is None or not doc.get("chat_ids"):
            return None
        default = E.declared_model(entry, access_id)
        return [m for m in ([default] if default in doc["chat_ids"] else []) + [i for i in doc["chat_ids"] if i != default]]
