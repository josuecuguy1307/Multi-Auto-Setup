"""REA (Reverse Engineer Anything) as the software-side adapter.

Only three REA commands are reachable, all static inspection (no capture, no scenario, no device, no
network feature): analyze-javascript-application, inspect-macho, inspect-artifact. Paths must sit under
human-configured roots. REA runs with an isolated HOME and an empty working directory.

What this does NOT enforce: a network ban. macOS `sandbox-exec` cannot launch the `ps` that REA's own
cleanup needs, so isolation is the command allowlist plus a measured check (see docs/mapping_cards/rea.md).

REA's evidence is mapped, not copied: its categorical confidence ("exact", "observed", "derived", "low")
becomes a float plus a status. Anything REA marks inferred or low confidence stays a `hypothesis`, and an
empty list is only reported when REA says its coverage is complete (otherwise the fact is `unknown`).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from envrec.adapters.base import Adapter, ToolContext
from envrec.contracts import (
    Coverage,
    CreatedBy,
    Evidence,
    Fact,
    FactStatus,
    Origin,
    ProbeResult,
    Target,
    TargetKind,
    Tier,
    ToolSpec,
    Unknown,
)
from envrec.store import Store

PROVIDER = "rea"
REA_CONFIDENCE = {"exact": 1.0, "observed": 0.95, "derived": 0.8, "high": 0.85, "medium": 0.6,
                  "inferred": 0.5, "low": 0.3}
MACHO_MAGICS = (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
                b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")
MAX_LIMITATIONS = 6


# ---- executors ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ReaRun:
    exit_code: int
    stdout: str
    stderr_tail: str = ""


class ReaExecutor(Protocol):
    def ready(self) -> bool: ...
    def run(self, argv: list[str], timeout_s: float) -> ReaRun: ...


class SubprocessReaExecutor:
    """Runs the real `rea` CLI. No shell; argv comes only from the allowlist below."""

    def __init__(self, rea_bin: Path | str, home_dir: Path | str, timeout_s: float = 180.0) -> None:
        self.rea_bin, self.home_dir, self.timeout_s = Path(rea_bin), Path(home_dir), timeout_s

    def ready(self) -> bool:
        return self.rea_bin.is_file() and os.access(self.rea_bin, os.X_OK)

    def run(self, argv: list[str], timeout_s: float | None = None) -> ReaRun:
        self.home_dir.mkdir(parents=True, exist_ok=True)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.home_dir), "LC_ALL": "C"}
        with tempfile.TemporaryDirectory(prefix="envrec-rea-") as cwd:
            try:
                done = subprocess.run([str(self.rea_bin), *argv], capture_output=True, text=True, cwd=cwd,
                                      env=env, timeout=timeout_s or self.timeout_s, check=False)
            except subprocess.TimeoutExpired:
                return ReaRun(124, "", "timed out")
            except OSError as exc:
                return ReaRun(127, "", f"cannot start rea: {exc.strerror or type(exc).__name__}")
        return ReaRun(done.returncode, done.stdout, done.stderr[-300:])


class RecordedReaExecutor:
    """Replays outputs recorded from a real REA run, keyed by the REA command name."""

    def __init__(self, outputs: dict[str, str], exit_codes: dict[str, int] | None = None) -> None:
        self.outputs, self.exit_codes, self.calls = outputs, exit_codes or {}, []

    @classmethod
    def from_dir(cls, directory: Path | str, mapping: dict[str, str],
                 exit_codes: dict[str, int] | None = None) -> RecordedReaExecutor:
        return cls({cmd: (Path(directory) / fname).read_text(encoding="utf-8") for cmd, fname in mapping.items()},
                   exit_codes)

    def ready(self) -> bool:
        return True

    def run(self, argv: list[str], timeout_s: float | None = None) -> ReaRun:
        self.calls.append(argv)
        cmd = argv[0]
        if cmd not in self.outputs:
            return ReaRun(2, "", f"no recording for {cmd}")
        return ReaRun(self.exit_codes.get(cmd, 0), self.outputs[cmd])


# ---- parsing REA evidence into our facts -------------------------------------------------------------

@dataclass
class FactSpec:
    key: str
    value: Any
    status: FactStatus
    confidence: float
    limitations: list[str] = field(default_factory=list)
    reason: str = ""  # why the value is unknown


@dataclass
class Parsed:
    facts: list[FactSpec]
    unknowns: list[tuple[str, str]]
    coverage: str  # complete | partial | unknown
    rea_confidence: str
    rea_limitations: list[str]
    rea_evidence_id: str
    provider_id: str
    operation: str


def confidence_of(label: Any) -> tuple[float, str | None]:
    if isinstance(label, str) and label in REA_CONFIDENCE:
        return REA_CONFIDENCE[label], None
    return 0.5, f"unmapped REA confidence {label!r}"


def _node_evidence(node: dict) -> dict:
    obs = (node.get("observations") or [{}])[0]
    return obs.get("evidence") or {}


def _label(node: dict) -> str | None:
    return (node.get("observations") or [{}])[0].get("label")


def _firm(nodes: list[dict]) -> bool:
    """True when every distinct label was directly observed by REA (some node with that label is
    observed + exact). An extra inferred node with the same label only adds relationships, it does not
    weaken what REA saw. A label that only inferred nodes carry is not firm."""
    by_label: dict[str | None, list[dict]] = {}
    for n in nodes:
        by_label.setdefault(_label(n), []).append(n)
    return all(any(_node_evidence(n).get("state") == "observed" and _node_evidence(n).get("confidence") == "exact"
                   for n in group) for group in by_label.values())


def _envelope(data: dict, facts: list[FactSpec], unknowns: list[tuple[str, str]], coverage: str) -> Parsed:
    return Parsed(facts, unknowns, coverage, str(data.get("confidence")), list(data.get("limitations") or []),
                  str(data.get("evidence_id", "")), (data.get("provider") or {}).get("id", "rea"),
                  str(data.get("operation", "")))


def parse_js_app(data: dict) -> Parsed:
    nr = data["normalized_result"]
    graph = nr["graph"]
    nodes = graph["nodes"]
    cov = graph.get("coverage") or {}
    complete = cov.get("status") == "complete" and not cov.get("truncated")
    top_conf, _ = confidence_of(data.get("confidence"))
    why = f"REA graph coverage is {cov.get('status')!r}"
    specs: list[FactSpec] = []

    def listed(key: str, labels: list[str], nodes_used: list[dict], *, empty_ok: bool = True) -> None:
        if not complete:
            specs.append(FactSpec(key, None, FactStatus.unknown, 0.0, reason=why))
        elif not labels and not empty_ok:
            specs.append(FactSpec(key, None, FactStatus.unknown, 0.0, reason="REA found none"))
        else:
            firm = _firm(nodes_used)
            specs.append(FactSpec(key, labels, FactStatus.tested if firm else FactStatus.hypothesis,
                                  top_conf if firm else confidence_of("low")[0],
                                  [] if firm else ["REA marks these as inferred, low confidence"]))

    pkg = next((n for n in nodes if n.get("kind") == "package"), None)
    if pkg is None:
        for key in ("app.name", "app.version", "app.entrypoint"):
            specs.append(FactSpec(key, None, FactStatus.unknown, 0.0, reason="no package.json in the app"))
    else:
        props = (pkg.get("observations") or [{}])[0].get("properties") or {}
        ev = _node_evidence(pkg)
        c, _ = confidence_of(ev.get("confidence"))
        st = FactStatus.tested if ev.get("state") == "observed" else FactStatus.hypothesis
        for key, field_name in (("app.name", "name"), ("app.version", "version"), ("app.entrypoint", "main")):
            val = props.get(field_name)
            specs.append(FactSpec(key, val, st, c) if val else
                         FactSpec(key, None, FactStatus.unknown, 0.0, reason=f"package.json has no {field_name!r}"))

    assets = [n for n in nodes if n.get("kind") == "javascript-asset"]
    files = sorted({_label(n) for n in assets if _label(n)})
    listed("app.source_files", files, assets, empty_ok=False)
    mods = [n for n in nodes if n.get("kind") == "javascript-module"]
    externals = sorted({_label(n) for n in mods if _label(n) and _label(n) not in files
                        and not _label(n).endswith(":default") and not _label(n).startswith(".")})
    listed("app.external_modules", externals, [n for n in mods if _label(n) in externals])
    eps = [n for n in nodes if n.get("kind") == "endpoint"]
    listed("app.endpoint_candidates", sorted({_label(n) for n in eps if _label(n)}), eps)
    unknowns = [(s.key, s.reason) for s in specs if s.status == FactStatus.unknown]
    return _envelope(data, specs, unknowns, "complete" if complete and not unknowns else "partial")


def parse_macho(data: dict) -> Parsed:
    nr = data["normalized_result"]
    c, _ = confidence_of(data.get("confidence"))
    specs = [FactSpec("app.format", nr.get("format"), FactStatus.tested, c),
             FactSpec("app.arch", (nr.get("selected_architecture") or {}).get("name"), FactStatus.tested, c)]
    partial = False
    for field_name, base, pick in (("dependencies", "app.dependencies", "path"), ("imports", "app.imports", "name"),
                                   ("exports", "app.exports", "name")):
        block = nr.get(field_name) or {}
        names = sorted({i[pick] for i in block.get("items", []) if i.get(pick)})
        exhaustive = bool(block.get("exhaustive"))
        partial |= not exhaustive
        specs.append(FactSpec(base if exhaustive else f"{base}_seen", names, FactStatus.tested, c,
                              [] if exhaustive else ["REA says this list is not exhaustive: more may exist"]
                              + list(block.get("limitations") or [])))
    for s in specs:
        if s.value in (None, ""):
            s.status, s.confidence, s.reason = FactStatus.unknown, 0.0, "REA gave no value"
    unknowns = [(s.key, s.reason) for s in specs if s.status == FactStatus.unknown]
    return _envelope(data, specs, unknowns, "partial" if partial or unknowns else "complete")


def parse_artifact(data: dict) -> Parsed:
    subject = data.get("subject") or {}
    c, _ = confidence_of(data.get("confidence"))
    sha = (subject.get("digest") or {}).get("sha256")
    specs = [FactSpec("app.sha256", sha, FactStatus.tested, c) if sha else
             FactSpec("app.sha256", None, FactStatus.unknown, 0.0, reason="REA gave no digest"),
             FactSpec("app.format", subject.get("format"), FactStatus.tested, c) if subject.get("format") else
             FactSpec("app.format", None, FactStatus.unknown, 0.0, reason="REA gave no format")]
    nr = data.get("normalized_result") or {}
    # REA's unexplored branches are its own record_unknown: what it did not check yet.
    unknowns = [("app.functions", str(b.get("detail") or b.get("reason") or "not analyzed")[:200])
                for b in nr.get("unexplored_branches", [])]
    status = (nr.get("coverage") or {}).get("status", "")
    coverage = "complete" if status.startswith("complete") and not unknowns else ("partial" if status else "unknown")
    unknowns += [(s.key, s.reason) for s in specs if s.status == FactStatus.unknown]
    return _envelope(data, specs, unknowns, coverage)


# ---- the allowlisted tools ------------------------------------------------------------------------------------

def _is_js_app_dir(p: Path) -> bool:
    return p.is_dir() and (p / "package.json").is_file()


def _is_macho(p: Path) -> bool:
    try:
        with p.open("rb") as handle:
            return handle.read(4) in MACHO_MAGICS
    except OSError:
        return False


@dataclass(frozen=True)
class ReaTool:
    name: str
    rea_command: str
    one_line: str
    produces: list[str]
    applies: Callable[[Path], bool]
    parse: Callable[[dict], Parsed]

    def argv(self, path: Path) -> list[str]:
        return [self.rea_command, str(path), "--json"]


REA_TOOLS: dict[str, ReaTool] = {t.name: t for t in (
    ReaTool("analyze_js_app", "analyze-javascript-application",
            "Statically analyze a JavaScript app folder: package, files, modules.",
            ["app.name", "app.version", "app.entrypoint", "app.source_files", "app.external_modules",
             "app.endpoint_candidates"], _is_js_app_dir, parse_js_app),
    ReaTool("inspect_binary", "inspect-macho",
            "Inspect a Mach-O program: architecture, libraries, imports, exports.",
            ["app.format", "app.arch", "app.dependencies", "app.imports_seen", "app.exports_seen"],
            lambda p: p.is_file() and _is_macho(p), parse_macho),
    ReaTool("inspect_artifact", "inspect-artifact",
            "Identify a file: hash, format, and what REA has not checked.",
            ["app.sha256", "app.format", "app.functions"], lambda p: p.is_file(), parse_artifact),
)}


class ReaAdapter(Adapter):
    name = "rea"

    def __init__(self, executor: ReaExecutor, allowed_roots: list[Path | str], timeout_s: float = 180.0) -> None:
        self.executor, self.timeout_s = executor, timeout_s
        self.allowed_roots = [Path(r).resolve() for r in allowed_roots]

    def tool_specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=t.name, ring=0, tier=Tier.read, target_kinds=[TargetKind.app], one_line=t.one_line,
                         origin=Origin.adapter, rung=1, source=self.name, produces=t.produces)
                for t in REA_TOOLS.values()]

    def _path_for(self, target: Target) -> Path | None:
        path = Path(target.handle).resolve()
        return path if any(path == r or r in path.parents for r in self.allowed_roots) else None

    def available(self, tool: str, target: Target, store: Store) -> bool:
        path = self._path_for(target)
        return bool(path and tool in REA_TOOLS and self.executor.ready() and path.exists()
                    and REA_TOOLS[tool].applies(path))

    async def run(self, tool: str, args: dict[str, Any], ctx: ToolContext) -> ProbeResult:
        spec = REA_TOOLS.get(tool)
        if spec is None:
            return ProbeResult(ok=False, error=f"REA adapter has no tool {tool!r}")
        path = self._path_for(ctx.target)
        if path is None:
            return ProbeResult(ok=False, error="that path is not under a root the human config allows; REA was not run")
        done = await asyncio.to_thread(self.executor.run, spec.argv(path), self.timeout_s)
        try:
            data = json.loads(done.stdout)
        except (ValueError, TypeError):
            return ProbeResult(ok=False, error=f"REA printed no JSON (exit {done.exit_code}): {done.stderr_tail[:120]}")
        if isinstance(data, dict) and "error" in data:
            return ProbeResult(ok=False, error=f"REA {data.get('code')}: {data.get('message')}"[:300],
                               raw_ref=ctx.store.put_raw(done.stdout))
        if done.exit_code != 0:
            return ProbeResult(ok=False, error=f"REA exited {done.exit_code} without an error object")
        try:
            parsed = spec.parse(data)
        except (KeyError, TypeError, IndexError) as exc:
            return ProbeResult(ok=False, error=f"REA output did not have the expected shape ({type(exc).__name__})")
        return self._to_result(spec, parsed, done.stdout, ctx)

    def _to_result(self, spec: ReaTool, p: Parsed, raw_text: str, ctx: ToolContext) -> ProbeResult:
        raw_ref = ctx.store.put_raw(raw_text)
        rea_limits = [lim[:200] for lim in p.rea_limitations[:MAX_LIMITATIONS]]
        facts: list[Fact] = []
        for s in p.facts:
            if s.status == FactStatus.unknown:
                facts.append(Fact(target_id=ctx.target.id, key=s.key, created_by=CreatedBy.adapter))
                continue
            _, unmapped = confidence_of(p.rea_confidence)
            facts.append(Fact(
                target_id=ctx.target.id, key=s.key, value=s.value, status=s.status, created_by=CreatedBy.adapter,
                evidence=[Evidence(
                    provider=PROVIDER, probe=f"{p.provider_id}:{p.operation}", event_id=ctx.event_id,
                    raw_ref=raw_ref, confidence=s.confidence,
                    limitations=[*s.limitations, *([unmapped] if unmapped else []), *rea_limits],
                    note=f"rea {p.rea_evidence_id[:24]}")]))
        have = {f.key for f in facts}
        for key, _ in p.unknowns:  # keys REA itself says it did not check become unknown facts too
            if key not in have:
                facts.append(Fact(target_id=ctx.target.id, key=key, created_by=CreatedBy.adapter))
                have.add(key)
        unknowns = [Unknown(key=k, question=q[:200]) for k, q in p.unknowns]
        return ProbeResult(ok=True, facts=facts, raw_ref=raw_ref, unknowns=unknowns, coverage=Coverage(
            scope=spec.name, status=p.coverage, checked=[f.key for f in facts if f.status != FactStatus.unknown],
            unknown=[u.key for u in unknowns]))
