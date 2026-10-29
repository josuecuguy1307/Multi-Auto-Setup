#!/usr/bin/env python3
"""R8 driver (the proof with the provisioned small model): same provisioning as r7_run.py, then scripts/prove_live.py through the tunnel.

R7 driver: provision the real runtime + a small model into a container over SSH, open the tunnel, verify the model, run
scripts/live_small_model.py through the tunnel, then close the tunnel and undo everything.

  python scripts/r7_run.py --tier qwen2.5-0.5b-q4km --repeats 5

Needs approved: true in config/runtime_manifest.yaml, Docker, and the envrec-fixture-sshd image. Artifacts come from the
manifest's smoke set (cached in .live-cache/); running this script is the go-ahead for exactly those artifacts.
"""
import argparse, json, os, shutil, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "build" / "worker"), str(ROOT / "scripts")]

from docker_helpers import container, exec_in, free_port, make_keypair, wait_ssh  # noqa: E402
from envrec.mission import EntryLimits, SshBlock, TargetConfig, TargetEntry, Transport  # noqa: E402
from envrec.remote.approval import ApprovalGate  # noqa: E402
from envrec.remote.provision import DeployPlan, Provisioner, Release  # noqa: E402
from envrec.remote.runner import RemoteRunner  # noqa: E402
from envrec.runtime import Fetcher, Manifest  # noqa: E402
from envrec.store import Store  # noqa: E402
from envrec.verify import ExpectedModel, TunnelClient  # noqa: E402
from envrec.verify.model_check import RemoteProbes, verify_model  # noqa: E402
import build as worker_build  # noqa: E402
import prove_live  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="qwen2.5-0.5b-q4km")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--ram", default="2g")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--max-steps", type=int, default=20)
    a = ap.parse_args()
    m = Manifest.load(ROOT / "config" / "runtime_manifest.yaml")
    m.require_approved()
    arts = m.smoke_artifacts()
    shas = {arts[k].sha256 for k in arts if k == a.tier or k.startswith("llama.cpp:")}
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp / "s")
    shown: list[str] = []
    fetch_gate = ApprovalGate(store, lambda _: "approve" if shown and any(s in shown[-1] for s in shas) else "deny", shown.append)
    f = Fetcher.from_manifest(m, ROOT / ".live-cache", fetch_gate)
    arch = "arm64" if os.uname().machine in ("arm64", "aarch64") else "amd64"
    rt = next(x for x in arts.values() if x.kind == "runtime" and x.meta["platform"] == f"linux/{arch}")
    md = arts[a.tier]
    src = next(s for s in m.raw["runtime_sources"] if s["id"] == rt.source)
    worker_build.build(tmp / "dist")
    release = Release.load(ROOT / "build" / "worker" / "release.json", tmp / "dist")
    plan = DeployPlan(rt, f.fetch(rt.id), md, f.fetch(md.id), kind=rt.source, binary_name=src["server_binary"], threads=2,
                      context=md.meta.get("context") or 2048, max_rss_mib=int(float(a.ram.rstrip("g")) * 1024 * 0.85), min_free_disk_mb=300)
    key, pub = make_keypair(tmp)
    port, known = free_port(), str(tmp / "known_hosts")
    os.environ["R7_SSH_KEY"] = key
    free0 = shutil.disk_usage(ROOT).free
    with container("envrec-fixture-sshd", "-m", a.ram, "-p", f"127.0.0.1:{port}:22", env={"PUBKEY": pub}) as name:
        wait_ssh(port, key, known)
        entry = TargetEntry(id="r7-box", name="R7 box", host="127.0.0.1", allowed_transports=[Transport.ssh],
                            ssh=SshBlock(user="owner", port=port, key_env="R7_SSH_KEY"),
                            limits=EntryLimits(max_disk_gb=20, max_model_params="8B"), owner_confirmed=True)
        cfg = TargetConfig(targets=[entry])
        gate = ApprovalGate(store, lambda _: "approve", lambda t: None)
        prov = Provisioner(RemoteRunner(cfg, host_key_policy="accept-new", known_hosts=known), gate, store, cfg, release)
        res = prov.deploy("r7-box", Transport.ssh, plan)
        if not res.ok:
            print("deploy failed:", res.failure)
            return 1
        tunnel = prov.open_tunnel("r7-box")
        try:
            exp = ExpectedModel(md.id, md.file)
            v = verify_model(TunnelClient(tunnel.local_port, timeout=60), RemoteProbes(prov.runner, "r7-box", Transport.ssh, res.facts["port"], res.facts["binary"]),
                             store, prov.targets["r7-box"], exp, startup_s=res.facts.get("startup_s"))
            print("verified over the tunnel:", v.ok, v.reason, "tokens/s", v.tokens_per_s, "rss MiB", v.rss_mib)
            if not v.ok:
                return 2
            sys.argv = ["prove_live.py", "--endpoint", f"http://127.0.0.1:{tunnel.local_port}/v1", "--label", "tiny", "--model-id", md.id, "--context", str(plan.context), "--human", "both", "--max-tokens", "900"]
            rc = prove_live.main()
        finally:
            tunnel.close()
            ok, msg = prov.undo("r7-box", Transport.ssh)
            print("undo:", ok, msg)
        print("controller disk used by this run (cache stays):", round((free0 - shutil.disk_usage(ROOT).free) / 1e6), "MB")
    return rc


if __name__ == "__main__":
    sys.exit(main())
