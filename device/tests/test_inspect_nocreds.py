"""P3-NOCREDS: inspection never collects credentials; planted secrets never reach the report or the model context
(MISSION 4.1 rule 8; D-040).

Two ways a credential could leak. (1) Reading a credential store: the inspector's allowlist names none (checked statically here and,
end to end, on a host planted with secrets in every usual store, process environments and command lines included). (2) A credential
sitting inside a field the inspector may read (a careless or hostile host puts a token in os-release or in a package database): every
string the collectors return passes mas_device.redact before anything is stored, and a value that looks like a credential is withheld
whole. Every planted secret is then searched for, raw and encoded, in every file the inspection wrote (full report, owner's text, agent
view, audit log). A stock Debian must lose nothing to the redactor (no false positives).

SIMULATED label (the software key); the hosts are containers of the pinned image (P2-PORTABLE).
"""

from __future__ import annotations

import base64
import json
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

import mas_device
from mas_device import approval as ap
from mas_device import inspect as ins
from mas_device import redact as rd
from mas_device import report as rp
from mas_device.gated import is_credential_path
from mas_device.keymodel import KeyModel

SRC = Path(mas_device.__file__).resolve().parents[1]  # the tree under test
TREE = SRC.parent
sys.path[:0] = [str(TREE / "device" / "medium"), str(Path(__file__).resolve().parent)]
import mas_dockerkit as dk  # noqa: E402
import mas_medium_build as md  # noqa: E402

LOCK = TREE / "device" / "medium" / "medium.lock.json"


def tok(prefix: str = "", n: int = 24) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return prefix + "".join(secrets.choice(alphabet) for _ in range(n))


def forms(s: str) -> list[bytes]:
    b = s.encode("utf-8")
    return [b, base64.b64encode(b), b.hex().encode(), s.encode("utf-16-le"), json.dumps(s).encode()[1:-1]]


# ---- what a credential looks like (the redactor, in-process) -------------------------------------------------------------------------------

def credential_examples() -> dict[str, str]:
    return {
        "aws_key_id": "AKIA" + "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(16)),
        "github": "ghp_" + tok(n=36),
        "github_pat": "github_pat_" + tok(n=40),
        "anthropic": "sk-ant-api03-" + tok(n=40),
        "openai": "sk-proj-" + tok(n=40),
        "slack": "xoxb-" + "".join(secrets.choice("0123456789") for _ in range(12)) + "-" + tok(n=24),
        "google": "AIza" + tok(n=35),
        "jwt": "eyJ" + tok(n=20) + "." + "eyJ" + tok(n=30) + "." + tok(n=40),
        "pem": "-----BEGIN OPENSSH PRIVATE KEY-----",
        "pem_rsa": "-----BEGIN RSA PRIVATE KEY-----",
        "assignment": "password=" + tok(n=12),
        "assignment_spaced": "api_key: " + tok(n=20),
        "url_userinfo": "https://deploy:" + tok(n=20) + "@git.example.invalid/repo.git",
    }


def test_the_redactor_withholds_every_kind_of_credential_whole():
    for kind, secret in credential_examples().items():
        for value in (secret, f"Debian {secret} edition", f"{secret}\n"):
            out = rd.redact_value(value)
            assert out == rd.WITHHELD, (kind, value, out)
            assert secret not in out


def test_the_redactor_leaves_ordinary_inventory_alone():
    ordinary = ["Debian GNU/Linux 12 (bookworm)", "debian", "12", "6.18.58.1-microsoft-standard-WSL2", "2:1.0~rc1-3", "1.2.13.dfsg-1",
                "amd64", "all", "libssl3", "ca-certificates", "02:42:ac:11:00:02", "Intel(R) Xeon(R) CPU @ 2.20GHz", "QEMU HARDDISK",
                "fixture-host", "lo", "eth0", "sk", "token", "password", "keyring", "gnupg", "openssh-client", "libpam-runtime",
                "debian-archive-keyring", "x" * 200, "AKIAPPLE", "eyJ", "ghp_short", "BEGIN", "base-passwd", "login.defs"]
    for v in ordinary:
        assert rd.redact_value(v) == v, v
    tree = {"a": ["Debian", {"b": "ghp_" + tok(n=36), "c": 7, "d": None, "e": True}], "f": "keep"}
    out, n = rd.redact(tree)
    assert n == 1 and out["a"][1]["b"] == rd.WITHHELD and out["a"][1]["c"] == 7 and out["f"] == "keep" and out["a"][0] == "Debian"
    assert tree["a"][1]["b"] != rd.WITHHELD  # the input is not modified


def test_the_allowlist_names_no_credential_store_and_no_process_data():
    for rel in ins.ALLOWED_FILES + ins.ALLOWED_DIRS:
        assert not is_credential_path(rel), rel
        parts = rel.split("/")
        assert parts[0] not in ("root", "home", "run", "tmp", "srv", "opt", "mnt", "media"), rel
        assert "environ" not in rel and "cmdline" not in rel and "ssl/private" not in rel and "shadow" not in rel, rel
        assert not (parts[0] == "proc" and parts[1:2] and (parts[1] == "*" or parts[1].isdigit() or parts[1] == "self")), rel
    assert len(rd.WITHHELD) <= 32  # fits every string limit of the report schema


# ---- end to end: hosts planted with secrets ---------------------------------------------------------------------------------------------

PLANT_STORES = """set -eu
useradd -m u >/dev/null 2>&1 || mkdir -p /home/u
echo 'mas:{shadow}:19000:0:99999:7:::' >> /etc/shadow
mkdir -p /root/.ssh /home/u/.aws /srv/app /etc/ssl/private /home/u/.mozilla/firefox/p /home/u/.config/gcloud
printf -- '-----BEGIN OPENSSH PRIVATE KEY-----\\n{ssh}\\n-----END OPENSSH PRIVATE KEY-----\\n' > /root/.ssh/id_ed25519
printf '[default]\\naws_access_key_id = {akia}\\naws_secret_access_key = {aws}\\n' > /home/u/.aws/credentials
printf 'API_KEY={env}\\n' > /srv/app/.env
printf '{pem}\\n' > /etc/ssl/private/server.key
printf 'https://u:{git}@example.invalid\\n' > /root/.git-credentials
printf '{{"logins":[{{"password":"{browser}"}}]}}\\n' > /home/u/.mozilla/firefox/p/logins.json
printf '{{"token":"{gcloud}"}}\\n' > /home/u/.config/gcloud/credentials.db
env MAS_SECRET={proc} sleep 600 &
sh -c 'sleep 600' plant {cmd} &
"""

PLANT_FIELDS = """set -eu
sed -i 's|^PRETTY_NAME=.*|PRETTY_NAME="Debian {akia_field} edition"|; s|^VERSION_ID=.*|VERSION_ID="{jwt_field}"|' /usr/lib/os-release
mkdir -p /lib/apk/db
printf 'P:{gh_field}\\nV:password={pw_field}\\nA:-----BEGIN RSA PRIVATE KEY-----\\n\\nP:musl\\nV:1.2.4\\nA:x86_64\\n\\nP:tool\\nV:{sk_field}\\nA:x86_64\\n' > /lib/apk/db/installed
"""


class Tick:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture(scope="module")
def lab():
    dk.docker_ok()
    root = TREE / "runs" / "nocreds-tests"
    root.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    clock = Tick()
    key = KeyModel.create(base / "key", clock=clock)
    medium = md.build(base / "medium", cache=dk.medium_cache(TREE), key_pub=key.public_key, tree=TREE)
    lab = dk.Lab(json.loads(LOCK.read_text(encoding="utf-8"))["base_image"], medium)
    lab.base, lab.key, lab.clock = base, key, clock
    yield lab
    lab.close()
    shutil.rmtree(base, ignore_errors=True)


def run_inspection(lab, name: str, script: str) -> Path:
    state = lab.base / f"state-{name}"
    state.mkdir()
    tools = lab.base / f"tools-{name}"
    tools.mkdir()
    (tools / "plant.sh").write_bytes(script.encode("ascii"))
    cmd = f"sh /tools/plant.sh && exec sh {dk.MOUNT}/launch.sh inspect --root / --state /state --target {name}"
    m = dk.Machine(lab, name, ["sh", "-c", cmd], rm=True, mounts=["-v", f"{state}:/state", "-v", f"{tools}:/tools:ro"])
    while (msg := m.line()) is not None:
        if "key_request" in msg:
            kr = msg["key_request"]
            req = ap.KeyRequest(*(bytes.fromhex(kr[n]) for n in ("session_id", "nonce", "action_hash", "undo_hash", "summary")),
                                kr["cls"], kr["press_window_s"])
            h = lab.key.request(req)
            lab.clock.t += 2
            lab.key.button.press()
            m.send({"token": lab.key.sign(h).hex()})
    rc, err, _ = m.finish()
    assert rc == 0, err[-3000:]
    return state


def outputs(state: Path) -> dict[str, bytes]:
    files = {p.name: p.read_bytes() for p in state.iterdir() if p.is_file() and not p.name.endswith(".lock")}
    assert any(n.startswith("inspect-") and n.endswith(".json") for n in files) and any(n.endswith(".md") for n in files)
    assert any(n.startswith("agentview-") for n in files) and "audit.log" in files
    return files


def leaks(files: dict[str, bytes], planted: dict[str, str]) -> list[tuple[str, str]]:
    return [(fname, what) for what, s in planted.items() for fname, data in files.items() if any(f in data for f in forms(s))]


def test_secrets_in_credential_stores_are_never_read(lab):
    planted = {"shadow": "$6$" + tok(n=40), "ssh": tok(n=60), "akia": credential_examples()["aws_key_id"], "aws": tok(n=40),
               "env": tok(n=32), "pem": tok(n=48), "git": tok(n=36), "browser": tok(n=24), "gcloud": tok(n=30), "proc": tok(n=28),
               "cmd": tok(n=28)}
    state = run_inspection(lab, "stores", PLANT_STORES.format(**planted))
    files = outputs(state)
    assert leaks(files, planted) == []
    rep = json.loads(next(v for k, v in files.items() if k.startswith("inspect-") and k.endswith(".json")))
    assert not any(is_credential_path(s) for s in rep["sources"]) and all(ins.allowed(s) for s in rep["sources"])
    assert rep["collectors"]["os"]["id"] == "debian"
    # nothing on a real Debian looked like a credential: no false positive
    assert rd.count_withheld(rep["collectors"]) == 0
    assert "withheld (looked like a credential): 0" in files[next(k for k in files if k.endswith(".md"))].decode("utf-8")


def test_secrets_inside_allowed_fields_are_withheld_everywhere(lab):
    ex = credential_examples()
    planted = {"akia_field": ex["aws_key_id"], "jwt_field": ex["jwt"], "gh_field": ex["github"], "pw_field": tok(n=16),
               "sk_field": ex["anthropic"]}
    state = run_inspection(lab, "fields", PLANT_FIELDS.format(**planted))
    files = outputs(state)
    assert leaks(files, planted) == []
    rep = json.loads(next(v for k, v in files.items() if k.startswith("inspect-") and k.endswith(".json")))
    c = rep["collectors"]
    assert c["os"]["pretty_name"] == rd.WITHHELD and c["os"]["version_id"] == rd.WITHHELD and c["os"]["id"] == "debian"
    apk = {p["name"]: p for p in c["packages_apk"]}
    assert apk["musl"] == {"name": "musl", "version": "1.2.4", "arch": "x86_64"}  # ordinary neighbours are kept
    assert apk[rd.WITHHELD]["version"] == rd.WITHHELD and apk[rd.WITHHELD]["arch"] == rd.WITHHELD
    assert apk["tool"]["version"] == rd.WITHHELD
    n = rd.count_withheld(c)
    assert n == 6
    md_text = files[next(k for k in files if k.endswith(".md"))].decode("utf-8")
    assert f"withheld (looked like a credential): {n}" in md_text
    view = json.loads(next(v for k, v in files.items() if k.startswith("agentview-")))
    assert rp.validate(view, rp.AGENT_SCHEMA) == [] and rp.validate(rep, rp.SCHEMA) == []
