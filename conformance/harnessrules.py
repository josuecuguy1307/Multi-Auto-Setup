"""Static scanners behind the HARNESS APP checks HA1-HA6 (docs/HARNESS_SPEC.md, docs/HARNESS_APP_STANDARD.md). Like `netrules`, they read source with `ast`/regex and never import or run it,
so the same function runs on the real tree and on the violating fixtures in `conformance/harnessfixtures/`."""
from __future__ import annotations

import ast
import re
from pathlib import Path

# HA1. An adapter translates. These names are what a rule is made of: who may see or decide what. They live in the core; an adapter that mentions one is deciding.
DECISION_WORDS = {"sensitivity", "confidential", "internal", "public", "OWNER", "CLAUDE-CODE", "model_in_middle", "cloud", "plan_hash_ok", "approved", "writes"}
FORBIDDEN_ADAPTER_IMPORTS = {"envrec_coordinator", "envrec_node", "envrec_net", "envrec", "envrec_os", "subprocess", "socket", "os", "shutil", "ctypes"}


def adapter_decision_sites(path: Path) -> list[str]:
    """Where an adapter compares, branches on or imports something that is the core's to decide. Words in docstrings and comments do not count; strings in code and names do."""
    path = Path(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    doc = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body and isinstance(n.body[0], ast.Expr)
           and isinstance(n.body[0].value, ast.Constant)}
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out += [f"{path.name}:{n.lineno}: import {a.name}" for a in n.names if a.name.split(".")[0] in FORBIDDEN_ADAPTER_IMPORTS]
        elif isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0] in FORBIDDEN_ADAPTER_IMPORTS:
            out.append(f"{path.name}:{n.lineno}: from {n.module} import ...")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in doc and n.value in DECISION_WORDS:
            out.append(f"{path.name}:{n.lineno}: {n.value!r}")
        elif isinstance(n, ast.Name) and n.id in DECISION_WORDS:
            out.append(f"{path.name}:{n.lineno}: {n.id}")
        elif isinstance(n, ast.Attribute) and n.attr in DECISION_WORDS:
            out.append(f"{path.name}:{n.lineno}: .{n.attr}")
    return out


# HA2. A channel with a model in the middle (or a cloud one) may list only mechanisms a model cannot press: never the owner panel's click, never the chat's own dialog unless no model sits between.
def channel_registry_violations(path: Path) -> list[str]:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
    consts = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str) for t in n.targets if isinstance(t, ast.Name)}

    def val(node):  # a literal, or a module-level string constant named in a tuple
        if isinstance(node, ast.Name):
            return consts[node.id]
        if isinstance(node, ast.Tuple):
            return tuple(val(e) for e in node.elts)
        return ast.literal_eval(node)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "Channel" and len(n.args) >= 5:
            name, cloud, mim, mech = (val(n.args[0]), val(n.args[2]), val(n.args[3]), val(n.args[4]))
            if (mim or cloud) and ("panel_click" in mech or (("host_dialog" in mech or "cli_typed" in mech) and mim)):
                out.append(f"{Path(path).name}:{n.lineno}: channel {name!r} has a model in the middle but lists {sorted(set(mech) & {'panel_click', 'host_dialog', 'cli_typed'})}")
    return out


# HA5. Cards load nothing: no URL, no remote font/script/image, no network call from a script, no import. (The names of the XML/HTML schemes are not used at all.)
EXTERNAL = re.compile(r"""https?://|(?<![\w.])//[a-z0-9.-]+\.[a-z]{2,}|@import|url\(\s*['"]?(?!#)|\bfetch\(|XMLHttpRequest|new WebSocket|importScripts|\bimport\(|<script[^>]+src=(?!["']?/[A-Za-z])|<link[^>]+href="https?|<iframe""")


def card_external_sites(path: Path) -> list[str]:
    out = []
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        code = line.split("#", 1)[0] if Path(path).suffix == ".py" and "'" not in line and '"' not in line else line
        if EXTERNAL.search(code):
            out.append(f"{Path(path).name}:{i}: {line.strip()[:80]}")
    return out


# HA11. Every decision of the owner is a choice, never free text. An intent of kind `text`, `progress` or `show_data` that puts a question or asks him to pick is a decision that nobody can answer
# with a tool: it must be an `ask_choice` (a native question where the host has one, numbered options otherwise) or a `confirm`.
ASK_PATTERNS = (re.compile(r"\?(\s|$)"), re.compile(r"(^|[.!:]\s+)(pick|choose|select|decide|tell me which|say which)\b", re.I))
FREE_TEXT_KINDS = {"text", "progress", "show_data"}


def asks_owner(text: str) -> bool:
    return any(p.search(text) for p in ASK_PATTERNS)


def _string_tables(paths: list[Path]) -> dict[str, str]:
    """Every English owner-facing string the given modules define, by its key or constant name: `STR["en"]` entries (also those added with `.update`) and module-level string constants. Read from source, never imported."""
    out: dict[str, str] = {}

    def literal(node):
        try:
            return ast.literal_eval(node)
        except (ValueError, SyntaxError):
            return None

    def take(d: ast.Dict):
        for k, v in zip(d.keys, d.values):
            kk, vv = literal(k) if k is not None else None, literal(v)
            if isinstance(kk, str) and isinstance(vv, str):
                out[kk] = vv
            elif isinstance(kk, str) and isinstance(vv, dict):
                out.update({a: b for a, b in vv.items() if isinstance(a, str) and isinstance(b, str)}) if kk == "en" else None
            elif isinstance(v, ast.Dict) and kk == "en":
                take(v)

    for path in paths:
        for n in ast.parse(Path(path).read_text(encoding="utf-8")).body:
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict):
                take(n.value)
            elif isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
                out.update({tg.id: n.value.value for tg in n.targets if isinstance(tg, ast.Name)})
            elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "update" and n.value.args and isinstance(n.value.args[0], ast.Dict):
                take(n.value.args[0])
    return out


def decision_without_choice_violations(source_files: list[Path], string_files: list[Path]) -> list[str]:
    """Where code emits an intent that is not a choice and whose words ask the owner to decide. The words are read from the call (a string, a `t(lang, "key")` or a constant of a strings module)."""
    table = _string_tables(string_files)
    out = []
    for path in source_files:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            name = call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id if isinstance(call.func, ast.Name) else ""
            if name == "_add" and len(call.args) >= 6:
                kind, text = call.args[1], call.args[5]
            elif name == "Intent":
                kw = {k.arg: k.value for k in call.keywords}
                kind, text = kw.get("kind"), kw.get("text_fallback")
            else:
                continue
            if not (isinstance(kind, ast.Constant) and kind.value in FREE_TEXT_KINDS) or text is None:
                continue
            words = []
            for n in ast.walk(text):
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    words.append(table.get(n.value, n.value))
                elif isinstance(n, ast.Name) and n.id in table:
                    words.append(table[n.id])
                elif isinstance(n, ast.Attribute) and n.attr in table:
                    words.append(table[n.attr])
            hit = next((w for w in words if asks_owner(w)), None)
            if hit:
                out.append(f"{Path(path).name}:{call.lineno}: a {kind.value} intent asks the owner to decide ({hit[:70]!r}): it must be an ask_choice")
    return out
