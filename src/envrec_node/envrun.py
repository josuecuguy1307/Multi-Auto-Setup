"""The node's side of `environment_run` (docs/NETWORK_CONTRACT.md 3.9 amended; docs/ROUTER_ENVIRONMENTS.md): `authorize` decides whether an environment may run, `run_environment` runs it, and nothing runs
before `authorize` passes. It is built like `envrec_node.setup`.

What a node never does here: take a command, a flag, a path or an executable name from the task. The task names an environment and carries its input as text; the executable, the arguments, the variables
the environment may receive (by NAME), how its answer is read and what keeps it in its workspace are all in the node's own copy of the catalog (data, `skills/router-environments`), and the hash of that
copy must match the signed token. An environment whose confinement is not one we trust runs only inside a sandbox backend; with none ready it is refused (`sandbox_missing`). Every environment works in
a per-task workspace under the node's own folder; whatever appears outside it while the environment ran is removed and the task fails (`outside_workspace`). That last check is DETECTIVE, and covers the
folder of workspaces only: the confinement itself is the tool's own switch (declared by its vendor, not verified by us) or our sandbox."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

import envrec_os
from envrec_net import envrun as E
from envrec_net import task as T
from envrec_os import procs


class EnvFailed(Exception):
    """An environment run that ended badly. `code` is a key of T.REJECTIONS (the typed reason)."""

    def __init__(self, code: str, text: str = "") -> None:
        super().__init__(text or code)
        self.code = code


def _family(ctx: dict) -> str:
    return ctx.get("os_family") or envrec_os.family()


def _backends(ctx: dict) -> list[str]:
    fn = ctx.get("sandbox_backends")
    return list(fn() if callable(fn) else (fn if fn is not None else procs.sandbox_backends()))


def authorize(params: T.EnvironmentRunParams, token: dict, ctx: dict) -> tuple[dict, dict]:
    """The node's checks, in this order, each with a typed reason: the token is scoped and carries an OWNER origin matching these parameters; this computer has the environment and its copy of the recipe
    is the one the token names; the operating system is one it runs on; the mode is the recipe's own for this task type; a confidential task is not for a cloud environment; the model endpoint (if any)
    is a loopback literal; a mode that needs a sandbox has one ready; the program is installed. Returns (entry, mode)."""
    E.check_env_scope(token, params)
    entry = (ctx.get("envs") or {}).get(params.environment_id)
    if entry is None:
        raise T.TokenRejected("unknown_environment")
    if E.recipe_sha256(entry) != params.recipe_sha256:
        raise T.TokenRejected("stale_recipe")
    fam = _family(ctx)
    if fam not in entry.get("os", []):
        raise T.TokenRejected("unsupported_os")
    mode = E.mode_of(entry, params.task_type)
    if mode is None or mode.get("name") != params.mode or mode.get("confinement") != token["scope"].get("confinement"):
        raise T.TokenRejected("bad_scope")
    if entry.get("cloud") and params.sensitivity == "confidential":
        raise T.TokenRejected("confidential_cloud")  # defense in depth: the router never sends this, the node refuses it anyway (R1)
    adapter = entry["adapter"]
    if params.endpoint or adapter.get("needs_model_endpoint") or adapter.get("kind") == "http_chat":
        try:
            E.parse_endpoint(params.endpoint or "")
        except ValueError:
            raise T.TokenRejected("not_loopback") from None
        if not params.model_id and adapter.get("kind") == "cli":
            raise T.TokenRejected("bad_parameters")
    if mode.get("confinement") in E.NEEDS_SANDBOX and not _backends(ctx):
        raise T.TokenRejected("sandbox_missing")
    if adapter.get("kind") == "cli":
        find = ctx.get("find_exe") or procs.find_exe
        if not find(adapter["exe"][fam]):
            raise T.TokenRejected("not_installed")
    return entry, mode


def _snapshot(root: Path, skip: str) -> dict[str, tuple[int, int]]:
    out = {}
    for p in root.iterdir():
        if p.name == skip:
            continue
        try:
            st = p.lstat()
        except OSError:
            continue
        out[p.name] = (st.st_mtime_ns, st.st_size)
    return out


def _inside(base: Path, target: Path) -> bool:
    return target == base or base in target.parents


def _list_files(ws: Path, hidden: set[str], limits: dict) -> list[dict]:
    base = ws.resolve()
    files = []
    for p in sorted(ws.rglob("*")):
        rel = p.relative_to(ws)
        if rel.parts[0] in hidden or p.is_dir():
            continue
        if p.is_symlink() or not _inside(base, p.resolve()):
            raise EnvFailed("outside_workspace", f"{rel} points outside the workspace")
        data = p.read_bytes()
        item = {"name": rel.as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if len(files) < int(limits["max_files"]):
            try:
                item["content"] = data.decode("utf-8")[: int(limits["max_file_chars"])]
            except UnicodeDecodeError:
                pass
        files.append(item)
    return files


def _read_answer(spec: dict, run: dict, ws: Path) -> tuple[str, list[str]]:
    kind, notes = spec.get("kind", "stdout"), []
    if kind == "file":
        f = ws / spec["name"]
        if f.is_file() and not f.is_symlink():
            return f.read_text(encoding="utf-8", errors="replace"), notes
        notes.append("the answer file was not written; the standard output is used")
    elif kind == "json_field":
        for line in reversed([x for x in run["stdout"].splitlines() if x.strip()]):
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict) and isinstance(doc.get(spec["field"]), str):
                return doc[spec["field"]], notes
            break
        notes.append("the standard output did not have the agreed form; it is used as it is")
    return run["stdout"], notes


def _wrap_sandbox(backend: str, policy: dict, argv: list[str], env_extra: dict[str, str], ws: Path) -> list[str]:
    spec = policy["sandbox_wrappers"][backend]
    head = [E.expand(a, {"workspace": str(ws)}).replace("{image}", spec["image"]) for a in spec["argv"]]
    flags = [x for k in sorted(env_extra) for x in ("-e", k)]  # by NAME: the value travels in the wrapper process's own environment, never in an argument list anyone can read
    return head[:-1] + flags + head[-1:] + argv  # flags before the image, then the program and its arguments (the image's name is the last element of the wrapper)


def run_environment(params: T.EnvironmentRunParams, entry: dict, mode: dict, ctx: dict) -> tuple[dict, list[dict]]:
    policy = ctx.get("policy") or E.load_policy()
    limits = policy["limits"]
    fam, adapter = _family(ctx), entry["adapter"]
    home = Path(ctx["mas_home"]).resolve()
    root = home / "workspaces"
    root.mkdir(parents=True, exist_ok=True)
    ws = root / params.workspace_id
    if ws.exists():
        raise EnvFailed("environment_failed", "the workspace already exists")
    ws.mkdir()
    before = _snapshot(root, ws.name)
    try:
        sandboxed = mode["confinement"] in E.NEEDS_SANDBOX
        values = {"workspace": "/work" if sandboxed else str(ws), "model": params.model_id or "", "endpoint": params.endpoint or ""}  # inside a sandbox the tool sees the workspace where the backend mounts it
        text = params.input_text
        run: dict[str, Any]
        if adapter["kind"] == "http_chat":
            run = _http_chat(params, adapter, ctx)
            answer, notes = run["stdout"], []
        else:
            for cf in adapter.get("config_files", []):
                target = (ws / cf["name"]).resolve()
                if not _inside(ws.resolve(), target):
                    raise EnvFailed("outside_workspace", "a config file would be outside the workspace")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(E.expand(cf["content"], values), encoding="utf-8")
            environ = ctx.get("environ")
            if environ is None:
                import os
                environ = os.environ
            env = {k: environ[k] for k in policy["env_base"] if k in environ}
            env.update({k: environ[k] for k in adapter.get("env_pass", []) if k in environ})  # by NAME: the value is the node's own and is never logged
            extra = {**adapter.get("env_set", {}), **mode.get("env_set", {})}
            env.update({k: E.expand(v, values) for k, v in extra.items()})
            prompt_arg = "Task:\n" + text  # a leading dash can never be read as a flag
            argv = [E.expand(a, {**values, "prompt": prompt_arg}) for a in mode["argv"]]
            find = ctx.get("find_exe") or procs.find_exe
            exe = find(adapter["exe"][fam]) or adapter["exe"][fam]
            full = [exe] + argv
            if sandboxed:
                backend = _backends(ctx)[0]
                full = _wrap_sandbox(backend, policy, [adapter["exe"][fam]] + argv, {k: v for k, v in env.items() if k in set(adapter.get("env_pass", [])) | set(extra)}, ws)
            runner = ctx.get("run") or procs.run_capture
            run = runner(full, ws, env, timeout_s=float(adapter.get("timeout_s", 600)), stdin_text=text if adapter.get("prompt_via") == "stdin" else None)
            answer, notes = _read_answer(adapter.get("output", {}), run, ws)
        after = _snapshot(root, ws.name)
        escaped = sorted(n for n in set(before) | set(after) if before.get(n) != after.get(n))
        if escaped:
            for n in escaped:
                victim = root / n
                if n in after and victim.is_file() and not victim.is_symlink():
                    victim.unlink()
                elif n in after and victim.is_symlink():
                    victim.unlink()
            raise EnvFailed("outside_workspace", f"{len(escaped)} entr{'y' if len(escaped) == 1 else 'ies'} appeared or changed outside the workspace")
        if run["timed_out"]:
            raise EnvFailed("environment_failed", "it did not finish in time")
        if run["exit"] != 0:
            raise EnvFailed("environment_failed", f"it ended with exit code {run['exit']}: {run['stderr'].strip()[-160:]}")
        files = _list_files(ws, {".home", ".last-message.txt"}, limits)
        result = {"environment": entry["id"], "mode": mode["name"], "confinement": mode["confinement"], "sandboxed": sandboxed, "exit_code": run["exit"], "duration_s": run["duration_s"],
                  "output_text": answer[: int(limits["max_output_chars"])], "files": files, "notes": notes}
        evidence = [{"provider": entry["id"], "tool": "exit code", "status": "tested"}, {"provider": "envrec_node.envrun", "tool": "workspace listing", "status": "tested",
                    "limitations": ["the check for writes outside the workspace is detective and covers the folder of workspaces only"]},
                    {"provider": entry["id"], "tool": "model output", "status": "hypothesis", "limitations": ["the text is the environment's own; nothing graded it here"] + notes}]
        return result, evidence
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def _http_chat(params: T.EnvironmentRunParams, adapter: dict, ctx: dict) -> dict:
    host, port = E.parse_endpoint(params.endpoint or "")
    url = f"http://{'[' + host + ']' if ':' in host else host}:{port}{adapter['chat_path']}"
    body = {"messages": [{"role": "user", "content": params.input_text}], "max_tokens": int(adapter["max_tokens"]), "temperature": 0, "stream": False}
    if params.model_id:
        body["model"] = params.model_id
    t0 = ctx.get("clock", time.monotonic)()
    try:
        with httpx.Client(timeout=float(adapter["timeout_s"]), transport=ctx.get("transport")) as c:
            r = c.post(url, json=body)
        if r.status_code != 200:
            raise EnvFailed("environment_failed", f"HTTP {r.status_code}")
        answer = str(r.json()["choices"][0]["message"]["content"]).strip()
    except httpx.HTTPError as exc:
        raise EnvFailed("environment_failed", type(exc).__name__) from None
    except (ValueError, KeyError, IndexError, TypeError):
        raise EnvFailed("environment_failed", "the answer did not have the agreed form") from None
    return {"exit": 0, "stdout": answer, "stderr": "", "timed_out": False, "duration_s": round(ctx.get("clock", time.monotonic)() - t0, 2)}
