"""W5-W8: the third task that writes, a credential change (docs/NETWORK_CONTRACT.md 3.10). Static checks over the source, each with a failing fixture:
W5 the coordinator sends it only after it found the owner's confirmation; W6 the node checks the signed scope before it touches the store and only `_credential_step` does; W7 a value is never put in a
log, a record or an error; W8 the credential module keeps nothing in a file and prints nothing (no plaintext fallback)."""
from __future__ import annotations

import ast
from pathlib import Path

import netrules

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _tree(text: str) -> ast.AST:
    return ast.parse(text)


def node_violations(text: str) -> list[str]:
    """W6: in `_credential_step` the scope check comes before `credentials.apply`; `apply` is called nowhere else in the module; `on_task` checks the scope of the write types before it accepts."""
    tree = _tree(text)
    out = []
    h = netrules._wfn(tree, "_credential_step")
    if h is None:
        return ["_credential_step not found"]
    chk = next((i for i, s in enumerate(h.body) if netrules._wcalls(s, attr="check_credential_scope")), None)
    app = next((i for i, s in enumerate(h.body) if netrules._wcalls(s, attr="apply")), None)
    if app is None:
        out.append("_credential_step never applies a change")
    elif chk is None or chk >= app:
        out.append("_credential_step touches the store without checking the signed scope first")
    elsewhere = [n for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name != "_credential_step" for n in netrules._wcalls(f, attr="apply")]
    if elsewhere:
        out.append("credentials.apply is called outside _credential_step")
    on_task = netrules._wfn(tree, "on_task")
    gate = [n for n in ast.walk(on_task) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "check_credential_scope"] if on_task is not None else []
    if on_task is None or not gate:
        out.append("on_task does not check a credential task's scope before it accepts it")
    return out


LOGGERS = {"log", "add", "_end", "_move", "record", "print", "info", "debug", "warning", "error", "send_event"}


def value_leak_violations(text: str, func: str) -> list[str]:
    """W7: in `func`, `value` is passed only to validation and to the one `send`: never to a log, a record, a print or the end of a task."""
    fn = netrules._wfn(_tree(text), func)
    if fn is None:
        return [f"{func} not found"]
    out = []
    for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
        name = call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id if isinstance(call.func, ast.Name) else ""
        if name in LOGGERS and any(isinstance(x, ast.Name) and x.id == "value" for a in [*call.args, *[k.value for k in call.keywords]] for x in ast.walk(a)):
            out.append(f"{func}:{call.lineno}: the value is passed to {name}")
    return out


def store_violations(text: str) -> list[str]:
    """W8: the credential module opens no file, writes no path, prints nothing and writes no environment variable."""
    out = []
    for n in ast.walk(_tree(text)):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in ("open", "write_text", "write_bytes", "print", "putenv", "dump", "mkstemp", "NamedTemporaryFile"):
                out.append(f"line {n.lineno}: {name}")
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Subscript) and "environ" in ast.unparse(t) for t in n.targets):
            out.append(f"line {n.lineno}: writes the environment")
    return out


def test_W5_the_coordinator_sends_a_credential_change_only_after_it_found_the_owners_confirmation():
    assert netrules.write_send_violations(SRC / "envrec_coordinator" / "tasks.py", func="start_credential_step", lookup="credential_approvals") == []
    assert "credential_step" in __import__("envrec_net.task", fromlist=["WRITE_TYPES"]).WRITE_TYPES  # so the generic start refuses it (W1)


def test_W5_fails_on_a_send_without_the_lookup_or_before_it(tmp_path):
    bad = tmp_path / "tasks.py"
    bad.write_text("class D:\n    def start(self, t):\n        return 1\n    def start_credential_step(self, node, name, action, h, value=None):\n        session = self.sessions(node)\n        session.send('task', {})\n")
    hits = " ".join(netrules.write_send_violations(bad, func="start_credential_step", lookup="credential_approvals"))
    assert "without looking for the owner's approval" in hits, hits
    late = tmp_path / "late.py"
    late.write_text("class D:\n    def start(self, t):\n        return 1\n    def start_credential_step(self, node, name, action, h, value=None):\n        session = self.sessions(node)\n        session.send('task', {})\n        a = self.credential_approvals(h, name, action)\n")
    assert "sends before" in " ".join(netrules.write_send_violations(late, func="start_credential_step", lookup="credential_approvals"))


def test_W6_the_node_checks_the_signed_scope_before_it_touches_the_store():
    assert node_violations((SRC / "envrec_node" / "tasks.py").read_text()) == []


def test_W6_fails_on_a_node_that_applies_without_the_check_or_applies_elsewhere():
    bad = "def _credential_step(p, ctx):\n    return credentials.apply(p.action, p.name, p.value)\ndef _other(p, ctx):\n    credentials.apply('set', 'X', 'v')\nasync def on_task(self, payload, send):\n    pass\n"
    hits = " ".join(node_violations(bad))
    assert "without checking the signed scope first" in hits and "called outside _credential_step" in hits and "on_task does not check" in hits, hits


def test_W7_the_value_never_reaches_a_log_a_record_or_the_end_of_a_task():
    assert value_leak_violations((SRC / "envrec_coordinator" / "tasks.py").read_text(), "start_credential_step") == []


def test_W7_fails_when_the_value_is_logged_or_recorded():
    bad = "class D:\n    def start_credential_step(self, node, name, action, h, value=None):\n        self.log('x', {'v': value})\n        self._end(rec, 'rejected', value)\n"
    assert len(value_leak_violations(bad, "start_credential_step")) == 2


def test_W8_the_credential_module_keeps_nothing_in_a_file_and_prints_nothing():
    assert store_violations((SRC / "envrec_node" / "credentials.py").read_text()) == []


def test_W8_fails_on_a_plaintext_fallback():
    bad = "import os\ndef set(name, value):\n    open('/tmp/k', 'w').write(value)\n    os.environ[name] = value\n    print(value)\n"
    assert len(store_violations(bad)) == 3
