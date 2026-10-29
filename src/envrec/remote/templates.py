"""The fixed command template library. The model never supplies a shell string.

A template is an argv list with named placeholders. Each placeholder has a strict schema checked before
substitution. Values are placed into argv elements; when a script needs a value, it receives it as a
positional parameter ($1, $2, ...), never interpolated into script text. Templates carry the tier that
decides whether an approval is needed (write tiers always are).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field

from envrec.contracts import Tier

INSTALL_ROOT = "/opt/envrec"
_META = set(";&|`$<>(){}[]*?!\\\"'\n\r\t ")


class TemplateError(ValueError):
    """A placeholder value or a template name was rejected."""


def _plain(value: object, pattern: str, what: str) -> str:
    s = str(value)
    if any(c in _META for c in s) or not re.fullmatch(pattern, s):
        raise TemplateError(f"{what} {s!r} is not allowed here")
    return s


def name_schema(v: object) -> str:
    return _plain(v, r"[A-Za-z0-9._\-]{1,64}", "name")


def port_schema(v: object) -> str:
    try:
        n = int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise TemplateError(f"port {v!r} is not a number") from None
    if not 1024 <= n <= 65535 or str(n) != str(v).strip():
        raise TemplateError(f"port {v!r} must be 1024 to 65535")
    return str(n)


def sha256_schema(v: object) -> str:
    return _plain(v, r"[0-9a-f]{64}", "sha256")


def install_path_schema(v: object) -> str:
    s = _plain(v, r"/[A-Za-z0-9._\-/]*", "path")
    if ".." in s.split("/") or "//" in s or not (s == INSTALL_ROOT or s.startswith(INSTALL_ROOT + "/")):
        raise TemplateError(f"path {s!r} must be inside {INSTALL_ROOT}")
    return s


def dir_probe_schema(v: object) -> str:
    s = _plain(v, r"/[A-Za-z0-9._\-/]*", "path")
    if s not in ("/", "/opt", INSTALL_ROOT) and not s.startswith(INSTALL_ROOT + "/"):
        raise TemplateError(f"path {s!r} may only be /, /opt or inside {INSTALL_ROOT}")
    if ".." in s.split("/"):
        raise TemplateError("'..' is not allowed")
    return s


Schema = Callable[[object], str]


@dataclass(frozen=True)
class Template:
    name: str
    argv: tuple[str, ...]
    tier: Tier
    placeholders: dict[str, Schema] = field(default_factory=dict)
    description: str = ""
    sends_stdin: bool = False  # a file transfer: the bytes go on stdin, never in argv

    def render(self, values: dict[str, object] | None = None) -> list[str]:
        values = dict(values or {})
        unknown = set(values) - set(self.placeholders)
        if unknown:
            raise TemplateError(f"template {self.name!r} has no placeholder(s) {sorted(unknown)}")
        missing = set(self.placeholders) - set(values)
        if missing:
            raise TemplateError(f"template {self.name!r} needs {sorted(missing)}")
        clean = {k: self.placeholders[k](v) for k, v in values.items()}
        out = []
        for part in self.argv:
            names = re.findall(r"\{(\w+)\}", part)
            for n in names:
                part = part.replace("{" + n + "}", clean[n])
            out.append(part)
        return out

    def remote_command(self, values: dict[str, object] | None = None) -> str:
        """The single string ssh sends to the remote shell: every argv element shell-quoted."""
        return " ".join(shlex.quote(p) for p in self.render(values))


def _t(name: str, argv: list[str], tier: Tier, ph: dict[str, Schema] | None = None, desc: str = "", stdin: bool = False) -> Template:
    return Template(name, tuple(argv), tier, ph or {}, desc, stdin)


_SH = "sh"
TEMPLATES: dict[str, Template] = {t.name: t for t in (
    # --- observe: read-only preflight and checks
    _t("uname_m", ["uname", "-m"], Tier.observe, desc="architecture"),
    _t("uname_s", ["uname", "-s"], Tier.observe, desc="operating system"),
    _t("os_release", ["cat", "/etc/os-release"], Tier.observe),
    _t("meminfo", ["cat", "/proc/meminfo"], Tier.observe, desc="RAM as the kernel reports it"),
    _t("cgroup_mem", [_SH, "-c", "cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || echo max"],
       Tier.observe, desc="RAM limit this process is actually held to"),
    _t("nproc", ["nproc"], Tier.observe),
    _t("df_free", ["df", "-Pk", "{path}"], Tier.observe, {"path": dir_probe_schema}, "free disk"),
    _t("gpu_probe", [_SH, "-c", "command -v nvidia-smi >/dev/null 2>&1 && echo nvidia || echo none"], Tier.observe),
    _t("pkg_manager", [_SH, "-c", "for p in apt-get apk dnf yum brew; do command -v $p >/dev/null 2>&1 && { echo $p; exit 0; }; done; echo none"],
       Tier.observe),
    _t("python_version", ["python3", "--version"], Tier.observe),
    _t("file_exists", [_SH, "-c", 'test -e "$1" && echo present || echo absent', _SH, "{path}"], Tier.observe, {"path": install_path_schema}),
    _t("sha256_file", ["sha256sum", "{path}"], Tier.observe, {"path": install_path_schema}),
    _t("health", ["python3", "-c", "import sys,urllib.request;print(urllib.request.urlopen('http://127.0.0.1:%s/health'%sys.argv[1],timeout=5).read().decode())", "{port}"],
       Tier.observe, {"port": port_schema}, "ask the localhost-bound server if it is up"),
    _t("chat_request", ["python3", "-c",
                        "import sys,json,urllib.request;r=urllib.request.Request('http://127.0.0.1:%s/v1/chat/completions'%sys.argv[1],data=sys.stdin.buffer.read(),headers={'content-type':'application/json'});print(urllib.request.urlopen(r,timeout=10).read().decode())",
                        "{port}"], Tier.observe, {"port": port_schema}, "send one test request to the local server", True),
    # --- write: every one needs a fresh approval
    _t("mkdir_install", ["mkdir", "-p", "{dir}"], Tier.write_approval, {"dir": install_path_schema}),
    _t("receive_file", ["dd", "of={dest}", "status=none"], Tier.write_approval, {"dest": install_path_schema}, "bytes arrive on stdin", True),
    _t("chmod_exec", ["chmod", "755", "{path}"], Tier.write_approval, {"path": install_path_schema}),
    _t("start_server", [_SH, "-c", 'nohup python3 "$1" serve --host 127.0.0.1 --port "$2" --model "$3" >"$4" 2>&1 & echo $! >"$5"; sleep 1; cat "$5"',
                        _SH, "{runtime}", "{port}", "{model}", "{log}", "{pidfile}"], Tier.write_approval,
       {"runtime": install_path_schema, "port": port_schema, "model": name_schema, "log": install_path_schema, "pidfile": install_path_schema},
       "start the model server bound to 127.0.0.1"),
    _t("stop_server", [_SH, "-c", 'if [ -f "$1" ]; then kill "$(cat "$1")" 2>/dev/null; rm -f "$1"; fi; echo stopped', _SH, "{pidfile}"],
       Tier.write_approval, {"pidfile": install_path_schema}),
    _t("remove_tree", ["rm", "-rf", "{dir}"], Tier.write_approval, {"dir": install_path_schema}, "undo: remove installed files"),
)}


def get_template(name: str) -> Template:
    try:
        return TEMPLATES[name]
    except KeyError:
        raise TemplateError(f"no template named {name!r}") from None
