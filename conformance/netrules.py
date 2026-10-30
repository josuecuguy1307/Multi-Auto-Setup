"""Static scanners behind the NETWORK checks N1-N3, N5, P1 and H1 (docs/NETWORK_CONTRACT.md, docs/HARNESS_SPEC.md). They read source with `ast`; they never import or run it.

Every function takes a directory (or file) and returns findings as `path:line: what`, so the same scanner runs on the real tree and on the violating fixtures in
`conformance/netfixtures/`, and the tests can show it fails where it must.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

# A node must not open ANY listening socket: these call names are all suspect in node code.
NODE_ATTRS = {"listen", "bind", "serve_forever", "start_server", "start_unix_server", "create_server", "serve", "run_app", "serve_unix", "create_datagram_endpoint"}
SERVER_CLASSES = {"HTTPServer", "ThreadingHTTPServer", "TCPServer", "ThreadingTCPServer", "UDPServer", "ThreadingUDPServer", "UnixStreamServer", "UnixDatagramServer",
                  "SimpleXMLRPCServer", "WSGIServer"}
SERVER_MODULES = {"uvicorn", "hypercorn", "waitress", "gunicorn", "aiohttp", "websockets"}  # `<module>.run(...)` / `.serve(...)` on these starts a listener
WILDCARDS = {"0.0.0.0", "::", ""}  # a bind to everything (the empty string means INADDR_ANY in socket.bind)
# What counts as ONE listening socket when the coordinator is measured (bind alone and serve_forever alone are the same socket as the call that makes it).
COUNT_ATTRS = {"listen", "start_server", "start_unix_server", "create_server", "serve", "run_app", "serve_unix", "create_datagram_endpoint"}
LOOPBACK_LITERALS = {"127.0.0.1", "::1", "[::1]"}  # `localhost` is NOT accepted: it depends on the machine's name resolution
DYNAMIC_EXEC_NAMES = {"exec", "eval", "compile"}
DYNAMIC_EXEC_ATTRS = {("runpy", "run_path"), ("runpy", "run_module"), ("util", "spec_from_file_location"), ("machinery", "SourceFileLoader")}
# Importing a module BY A FIXED NAME inside our own package (`__import__("envrec.recipes.model")`, `importlib.import_module(f"envrec.tools.builtin.{m.name}")` over the package's own
# directory) loads reviewed code, not model-written code, so it is not listed; loading code from a PATH, or exec/eval/compile, is. Reviewed exceptions go here as "file.py:name".
DYNAMIC_EXEC_ALLOW: set[str] = set()


def _files(root: Path) -> list[Path]:
    root = Path(root)
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts) if root.exists() else []


def _name(func: ast.expr) -> tuple[str | None, str | None]:
    """(receiver name, called name) of a call target."""
    if isinstance(func, ast.Name):
        return None, func.id
    if isinstance(func, ast.Attribute):
        recv = func.value.id if isinstance(func.value, ast.Name) else (func.value.attr if isinstance(func.value, ast.Attribute) else None)
        return recv, func.attr
    return None, None


def _calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def listen_sites(root: Path, *, wildcard: bool = True) -> list[str]:
    """Every call that can open a listening socket (and, with `wildcard`, every string that is a bind-to-everything address). For nodes: must be empty."""
    out = []
    for p in _files(root):
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                recv, name = _name(node.func)
                if name in NODE_ATTRS or name in SERVER_CLASSES or (recv in SERVER_MODULES and name in ("run", "serve", "Server", "start_server", "run_app")):
                    out.append(f"{p.name}:{node.lineno}: {(recv + '.') if recv else ''}{name}(...) can open a listening socket")
            elif wildcard and isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in ("0.0.0.0", "::"):
                out.append(f"{p.name}:{node.lineno}: wildcard address {node.value!r}")
    return out


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` assignments, so a bind written as `(WEB_HOST, port)` can still be read as the literal it is."""
    out = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            out.update({t.id: n.value.value for t in n.targets if isinstance(t, ast.Name)})
    return out


def _host_of_site(node: ast.Call, consts: dict[str, str]) -> str | None:
    """The literal bind host of a listening call, or None when it is not a literal (a variable, an attribute, a computed value)."""
    recv, name = _name(node.func)
    expr = next((k.value for k in node.keywords if k.arg == "host"), None)
    if expr is None and (name in SERVER_CLASSES or (recv in SERVER_MODULES and name in ("Server",))):
        addr = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "server_address"), None)
        if isinstance(addr, ast.Tuple) and addr.elts:
            expr = addr.elts[0]  # ThreadingHTTPServer((host, port), Handler)
    elif expr is None and name in ("start_server", "create_server", "start_unix_server") and len(node.args) > 1:
        expr = node.args[1]  # asyncio.start_server(cb, host, port)
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.Name):
        return consts.get(expr.id)
    return None


def listener_sites(root: Path) -> list[tuple[str, str]]:
    """Every site that creates one listening socket, with its kind: 'loopback' when its bind host is a loopback literal written in the code, 'open' for anything else (a variable the
    owner chooses, a wildcard, an address, or a call whose host cannot be read: all of those could face the LAN)."""
    out = []
    for p in _files(root):
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=str(p))
        consts = _module_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            recv, name = _name(node.func)
            if name in COUNT_ATTRS or name in SERVER_CLASSES or (recv in SERVER_MODULES and name in ("run", "serve", "Server", "start_server", "run_app")):
                kind = "udp" if name == "create_datagram_endpoint" else "loopback" if _host_of_site(node, consts) in LOOPBACK_LITERALS else "open"
                out.append((f"{p.name}:{node.lineno}: {(recv + '.') if recv else ''}{name}(...) [{kind}]", kind))
    return out


def count_listeners(root: Path) -> list[str]:
    """The sites that each create one listening socket."""
    return [d for d, _ in listener_sites(root)]


def coordinator_verdict(root: Path) -> tuple[bool, str]:
    """(ok, detail). No coordinator code yet: nothing to measure, so ok. With code (docs/NETWORK_CONTRACT.md 1.1, 1.7, 1.8): exactly ONE LAN-facing TCP listening site (its bind host is
    chosen by the owner, so it is not a loopback literal), AT MOST ONE loopback-only TCP site (the owner's web screen) and AT MOST ONE UDP discovery site. A second TCP site that is not a
    loopback literal fails, whatever it serves; so does a second UDP site."""
    if not Path(root).exists() or not _files(root):
        return True, "no coordinator code in the tree yet (nothing to measure)"
    sites = listener_sites(root)
    lan = [d for d, k in sites if k == "open"]
    loop = [d for d, k in sites if k == "loopback"]
    udp = [d for d, k in sites if k == "udp"]
    detail = (f"{len(lan)} LAN-facing listening site(s) (must be exactly 1), {len(loop)} loopback-only site(s) (at most 1), {len(udp)} UDP discovery site(s) (at most 1): "
              + "; ".join(d for d, _ in sites))
    if not sites:
        detail = "0 listening sites in the coordinator code (it must open exactly one LAN-facing socket)"
    return len(lan) == 1 and len(loop) <= 1 and len(udp) <= 1, detail


def udp_socket_sites(root: Path) -> list[str]:
    """Every `socket.socket(..., SOCK_DGRAM)` creation: the node's one short-lived discovery socket is the only one allowed in node code, and only in its discovery module."""
    out = []
    for p in _files(root):
        for node in _calls(p):
            recv, name = _name(node.func)
            if name == "socket" and recv in ("socket", None) and any("SOCK_DGRAM" in ast.unparse(a) for a in node.args):
                out.append(f"{p.name}:{node.lineno}: UDP socket")
    return out


def dynamic_exec_sites(root: Path) -> list[str]:
    """exec / eval / compile / runpy / loading code from a path: forbidden in src/ outside a reviewed allow-list."""
    out = []
    for p in _files(root):
        for node in _calls(p):
            recv, name = _name(node.func)
            hit = (recv is None and name in DYNAMIC_EXEC_NAMES) or ((recv, name) in DYNAMIC_EXEC_ATTRS)
            if hit and f"{p.name}:{name}" not in DYNAMIC_EXEC_ALLOW:
                out.append(f"{p.name}:{node.lineno}: {(recv + '.') if recv else ''}{name}(...)")
    return out


def _contains_call_of(node: ast.AST, pred) -> bool:
    return any(isinstance(n, ast.Call) and pred(*_name(n.func)) for n in ast.walk(node))


def gateway_violations(path: Path, func: str, *, refusal: str = "SandboxUnavailable") -> list[str]:
    """The sandbox gateway `func` in `path` must refuse BEFORE it starts any process: a statement that raises `refusal` under a check whose name says
    ready/available/backend must come before the first statement that calls `subprocess`."""
    path = Path(path)
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    fn = next((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func), None)
    if fn is None:
        return [f"{path.name}: gateway function {func} not found"]

    def raises_guarded(stmt: ast.stmt) -> bool:
        for n in ast.walk(stmt):
            if isinstance(n, ast.If) and _contains_call_of(n.test, lambda r, c: bool(c) and any(w in c.lower() for w in ("ready", "available", "backend"))):
                for r in ast.walk(n):
                    if isinstance(r, ast.Raise) and r.exc is not None and refusal in ast.unparse(r.exc):
                        return True
        return False

    def spawns(stmt: ast.stmt) -> bool:
        return _contains_call_of(stmt, lambda r, c: r == "subprocess" or c in ("Popen", "check_output", "system", "run_in_executor", "create_subprocess_exec"))

    guard = next((i for i, s in enumerate(fn.body) if raises_guarded(s)), None)
    spawn = next((i for i, s in enumerate(fn.body) if spawns(s)), None)
    if guard is None:
        return [f"{path.name}:{fn.lineno}: {func} has no 'raise {refusal}' under a ready/available/backend check"]
    if spawn is not None and spawn < guard:
        return [f"{path.name}:{fn.body[spawn].lineno}: {func} starts a process before it checks that a sandbox backend is ready"]
    return []


def references(root: Path, name: str, *, except_file: str) -> list[str]:
    """Uses of a gateway-internal name (e.g. docker_argv) outside the gateway's own file: a way round the gateway."""
    out = []
    for p in _files(root):
        if p.name == except_file:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=str(p))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Name) and n.id == name) or (isinstance(n, ast.Attribute) and n.attr == name) or (isinstance(n, ast.alias) and n.name == name):
                out.append(f"{p.name}:{getattr(n, 'lineno', 0)}: {name}")
    return out


# ---- N5: inference endpoints used by node code are loopback literals --------------------------------------------------------------------------------
ENDPOINT_NAMES = {"endpoint", "base_url", "api_base", "inference_url", "model_url", "server_url", "host"}
_URL = re.compile(r"^(?:https?|wss?)://(\[[^\]]+\]|[^/:?#\s{}]*)")


def _bad_endpoint(value: str) -> str | None:
    """None if the string is not an endpoint or is a loopback literal; else the offending host."""
    m = _URL.match(value)
    if m:
        host = m.group(1)
        return None if host == "" or host in LOOPBACK_LITERALS else host  # an empty host is a formatted one (f"http://{host}"): the name it comes from is checked where it is set
    if "://" in value or not value or " " in value:
        return None  # another scheme (a CLI route such as claude-cli://) or free text
    return None if value in LOOPBACK_LITERALS else value


def endpoint_sites(root: Path) -> list[str]:
    """Python: any URL string with a non-loopback host, and any endpoint-named variable, keyword or default whose literal value is not a loopback host."""
    out = []
    for p in _files(root):
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                bad = _URL.match(node.value) and _bad_endpoint(node.value)
                if bad:
                    out.append(f"{p.name}:{node.lineno}: URL with host {bad!r}")
            pairs = []
            if isinstance(node, ast.keyword) and node.arg:
                pairs.append((node.arg, node.value))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                for tg in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    name = tg.id if isinstance(tg, ast.Name) else (tg.attr if isinstance(tg, ast.Attribute) else None)
                    if name:
                        pairs.append((name, node.value))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args.args[-len(node.args.defaults):] if node.args.defaults else []
                pairs += [(a.arg, d) for a, d in zip(args, node.args.defaults)]
                pairs += [(a.arg, d) for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults) if d is not None]
            for name, val in pairs:
                if name.lower() in ENDPOINT_NAMES and isinstance(val, ast.Constant) and isinstance(val.value, str):
                    bad = _bad_endpoint(val.value)
                    if bad and not _URL.match(val.value):  # URLs were reported above
                        out.append(f"{p.name}:{val.lineno}: {name} = {val.value!r}")
    return out


def config_endpoint_sites(path: Path) -> list[str]:
    """YAML configuration: any endpoint-named key (at any depth) whose value is not a loopback literal host or URL."""
    out: list[str] = []

    def walk(node, trail=""):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and str(k).lower() in ENDPOINT_NAMES:
                    bad = _bad_endpoint(v)
                    if bad:
                        out.append(f"{Path(path).name}: {trail}{k} = {v!r} (host {bad!r})")
                else:
                    walk(v, f"{trail}{k}.")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{trail}{i}.")

    walk(yaml.safe_load(Path(path).read_text()) or {})
    return out


# ---- P1: no OS-specific imports or calls outside the per-OS layer -------------------------------------------------------------------------------
OS_ONLY_MODULES = {"winreg", "msvcrt", "_winapi", "winerror", "fcntl", "termios", "tty", "pty", "pwd", "grp", "resource", "syslog", "posix", "nt", "AppKit", "Foundation", "objc", "Quartz",
                   "CoreFoundation", "win32api", "win32con", "win32file", "win32security", "pywintypes", "win32com"}
OS_ONLY_CALLS = {("os", n) for n in ("getuid", "geteuid", "getgid", "getegid", "setuid", "setgid", "fork", "chown", "startfile", "chmod", "umask", "mkfifo", "symlink", "uname", "getpgid", "killpg")}
PLATFORM_PROBES = {("platform", n) for n in ("system", "win32_ver", "mac_ver", "libc_ver", "freedesktop_os_release", "uname")} | {("sys", "getwindowsversion")}
SPAWNERS = {("subprocess", n) for n in ("run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput")} | {("os", "system"), ("os", "popen"), ("asyncio", "create_subprocess_exec"), ("asyncio", "create_subprocess_shell")}


def os_specific_sites(root: Path) -> list[str]:
    out = []
    for p in _files(root):
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out += [f"{p.name}:{node.lineno}: import {a.name}" for a in node.names if a.name.split(".")[0] in OS_ONLY_MODULES or a.name.startswith("ctypes.wintypes")]
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in OS_ONLY_MODULES or node.module.startswith("ctypes.wintypes"):
                    out.append(f"{p.name}:{node.lineno}: from {node.module} import ...")
                elif node.module == "ctypes" and any(a.name in ("windll", "WinDLL", "wintypes") for a in node.names):
                    out.append(f"{p.name}:{node.lineno}: from ctypes import windll")
            elif isinstance(node, ast.Call):
                recv, name = _name(node.func)
                if (recv, name) in OS_ONLY_CALLS | PLATFORM_PROBES:
                    out.append(f"{p.name}:{node.lineno}: {recv}.{name}(...)")
                elif (recv, name) in SPAWNERS:
                    out.append(f"{p.name}:{node.lineno}: {recv}.{name}(...) runs a program: which program, and with what, is per-OS knowledge")
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and ((node.value.id, node.attr) in {("sys", "platform"), ("os", "name"), ("ctypes", "windll")}):
                out.append(f"{p.name}:{node.lineno}: {node.value.id}.{node.attr}")
    return out


# ---- H1: no OS- or tool-specific knowledge outside skills and the per-OS layer ----------------------------------------------------------------------
KNOWLEDGE = re.compile(
    r"^(/usr/|/opt/|/etc/|/var/|/proc/|/sys/|/Library/|/Applications/|/System/|~/Library|~/\.config|~/\.local|~/\.ssh|[A-Za-z]:\\|%[A-Za-z_]+%|\$\{?XDG)"  # install and state paths
    r"|Program Files|AppData|HKEY_|\\SOFTWARE\\|\.plist\b|\.service\b"  # a Windows install folder, registry keys, a macOS property list, a systemd unit
    r"|\b(systemctl|launchctl|journalctl|powershell|pwsh|cmd\.exe|netsh|icacls|apt-get|apt install|brew install|dnf install|yum install|winget|choco install|sudo|sc\.exe|reg add|reg query|schtasks)\b",
    re.I)


def knowledge_sites(root: Path) -> list[str]:
    out = []
    for p in _files(root):
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=str(p))
        docstrings = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
                      and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant) and isinstance(n.body[0].value.value, str)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings and KNOWLEDGE.search(node.value):
                out.append(f"{p.name}:{node.lineno}: {node.value[:60]!r}")
    return out


# ---- T1: a remote task can only run what the node's read-only allowlist names; no shell, no lookup by name ----------------------------------------------------------------
FORBIDDEN_TASK_MODULES = {"subprocess", "os", "runpy", "importlib", "shlex", "pty", "ctypes", "socket", "multiprocessing"}
FORBIDDEN_TASK_CALLS = {"eval", "exec", "compile", "getattr", "__import__", "globals", "locals", "vars", "open", "setattr"}


def task_dispatch_violations(path: Path) -> list[str]:
    """The node's task dispatcher must (1) import no module that can start a program or load code, (2) call none of the dynamic-lookup builtins, and (3) hold its allowlist as ONE dict literal
    `ALLOWED = {"name": function, ...}` whose values are plain names, which is the only thing a task type is looked up in."""
    path = Path(path)
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out += [f"{path.name}:{n.lineno}: import {a.name}" for a in n.names if a.name.split(".")[0] in FORBIDDEN_TASK_MODULES]
        elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in FORBIDDEN_TASK_MODULES:
            out.append(f"{path.name}:{n.lineno}: from {n.module} import ...")
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in FORBIDDEN_TASK_CALLS:
            out.append(f"{path.name}:{n.lineno}: {n.func.id}(...)")
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("system", "popen", "run", "Popen", "check_output", "import_module", "spawn"):
            out.append(f"{path.name}:{n.lineno}: .{n.func.attr}(...)")
    allowed = [n for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign)) and any(isinstance(t, ast.Name) and t.id == "ALLOWED" for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
    if len(allowed) != 1 or not isinstance(allowed[0].value, ast.Dict) or not all(isinstance(k, ast.Constant) and isinstance(v, ast.Name) for k, v in zip(allowed[0].value.keys, allowed[0].value.values)):
        out.append(f"{path.name}: the allowlist must be one dict literal ALLOWED = {{\"task\": function}}")
    return out


# ---- clock: no validation path inside a session may reject solely on wall-clock time --------------------------------------------------------------------------------------
CLOCK_NAMES = {"stale_timestamp", "WINDOW_S", "window_s", "Verifier"}  # `Verifier` is the time-window class; sessions use `SessionVerifier`


def session_clock_sites(paths: list[Path]) -> list[str]:
    """Names and strings in session code that mean 'compare a message's timestamp with the wall clock'."""
    out = []
    for p in paths:
        p = Path(p)
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=str(p))
        docs = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and n.id in CLOCK_NAMES:
                out.append(f"{p.name}:{n.lineno}: {n.id}")
            elif isinstance(n, ast.Attribute) and n.attr in CLOCK_NAMES:
                out.append(f"{p.name}:{n.lineno}: .{n.attr}")
            elif isinstance(n, ast.alias) and n.name in CLOCK_NAMES:
                out.append(f"{p.name}:{getattr(n, 'lineno', 0)}: import {n.name}")
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs and "stale_timestamp" in n.value:
                out.append(f"{p.name}:{n.lineno}: 'stale_timestamp'")
            elif isinstance(n, ast.Compare) and any(isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id == "abs" and "ts" in ast.unparse(x) for x in [n.left, *n.comparators]):
                out.append(f"{p.name}:{n.lineno}: abs(... ts ...) compared")
    return out


# ---- the agent's pairing offer: not without an OWNER pick -----------------------------------------------------------------------------------------------------------------
def owner_pick_violations(path: Path, func: str = "_offer") -> list[str]:
    """`func` makes the pairing code (a call to `offer_for_waiting`). Before that call it must call `require_owner_pick` and return early when that gives a refusal."""
    path = Path(path)
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    fn = next((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func), None)
    if fn is None:
        return [f"{path.name}: function {func} not found"]

    def calls(stmt, attr):
        return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr for n in ast.walk(stmt))

    pick = next((i for i, s in enumerate(fn.body) if calls(s, "require_owner_pick")), None)
    offer = next((i for i, s in enumerate(fn.body) if calls(s, "offer_for_waiting")), None)
    if offer is None:
        return [f"{path.name}:{fn.lineno}: {func} never makes an offer (nothing to guard: the check is looking at the wrong function)"]
    if pick is None:
        return [f"{path.name}:{fn.lineno}: {func} makes a pairing offer without checking for the owner's pick"]
    if pick > offer or (pick == offer):
        return [f"{path.name}:{fn.body[offer].lineno}: {func} makes the pairing offer before it checks for the owner's pick"]
    guard = fn.body[pick + 1] if pick + 1 < len(fn.body) else None
    if not (isinstance(guard, ast.If) and any(isinstance(n, ast.Return) for n in ast.walk(guard))):
        return [f"{path.name}:{fn.body[pick].lineno}: the owner-pick check is not followed by an early return"]
    return []
