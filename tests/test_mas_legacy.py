"""The retired SSH-key-derived pairing code (and the whole inbound-SSH door) must not be reachable from the new commands."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RETIRED = ("envrec.doors", "envrec.remote", "envrec.tools.builtin.doors")


def test_importing_everything_the_mas_commands_use_never_loads_the_retired_door_code():
    code = (
        "import sys\n"
        "import envrec_mas.cli, envrec_net.pake, envrec_net.protocol, envrec_net.tls, envrec_coordinator.server, envrec_coordinator.pairing, envrec_node.agent, envrec_node.pair\n"
        "from envrec_mas.cli import main\n"
        f"bad = [m for m in sys.modules if m == 'envrec_doors' or any(m == r or m.startswith(r + '.') for r in {RETIRED!r})]\n"
        "print('LOADED', bad)\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"})
    assert done.returncode == 0, done.stderr[-400:]
    assert done.stdout.strip() == "LOADED []", done.stdout


def test_the_retired_pairing_code_is_marked_legacy_where_it_lives():
    for rel in ("src/envrec/doors/pair_code.py", "src/envrec/doors/pin.py", "src/envrec/doors/windows_door.py", "src/envrec/tools/builtin/doors.py", "skills/protocol-door/SKILL.md"):
        assert "LEGACY" in (ROOT / rel).read_text()[:700], rel
