"""Run the skill library the way discovery needs it: survey the observe-tier skills, run a named one, record a refusal.

A discovery "adapter" is a skill with a plan. Nothing in this module knows about any device."""

from __future__ import annotations

from collections.abc import Callable

from envrec.contracts import Target
from envrec.discovery.collectors import Collector
from envrec.discovery.runner import AdapterRun, _finish
from envrec.discovery.sanitize import Salts
from envrec.skills.lib import Library
from envrec.skills.runner import SKILL_TIMEOUT_S, _Info, run_skill
from envrec.skills.skillmd import Skill
from envrec.store import Store
from envrec.tools.ctx import ToolContext, flush_approvals

_LIB: Library | None = None


def library() -> Library:
    global _LIB
    if _LIB is None:
        _LIB = Library().load()
    return _LIB


def all_adapters(platform: str | None = None) -> list[Skill]:
    """Every builtin skill with a plan that discovery runs, ordered by ring then id."""
    skills = [s for s in library().runnable() if s.meta.provenance == "builtin" and (platform is None or "any" in s.meta.platforms or platform in s.meta.platforms)]
    return sorted(skills, key=lambda s: (s.meta.ring, s.id))


def get_adapter(skill_id: str) -> Skill | None:
    return library().get(skill_id)


def mode_of(skill: Skill) -> str:
    return skill.mode


def run_adapter(skill: Skill | str, collector: Collector, store: Store, target: Target, *, salts: Salts, platform: str | None = None, params: dict | None = None,
                event_id: str | None = None, timeout_s: float = SKILL_TIMEOUT_S, approve: Callable[[str], bool] | None = None, script_runner=None) -> AdapterRun:
    """Run one skill. `params` are run-level settings the tools read (seconds, scope, rate, hosts, port); those the plan names as `{param.x}` reach its steps."""
    sk = get_adapter(skill) if isinstance(skill, str) else skill
    platform = platform or collector.platform
    ctx = ToolContext(collector=collector, platform=platform, store=store, target=target, salts=salts, params={"salt": salts.run, **(params or {})}, approve=approve, event_id=event_id)
    kw = {"script_runner": script_runner} if script_runner is not None else {}
    try:
        return run_skill(sk, ctx, params=dict(params or {}), timeout_s=timeout_s, **kw)
    finally:
        flush_approvals(ctx)  # approvals asked on a worker thread are written here, on the thread that owns the store


def survey(skills: list[Skill], collector: Collector, store: Store, target: Target, *, salts: Salts, platform: str | None = None,
           params: dict[str, dict] | None = None, approve: Callable[[str], bool] | None = None) -> list[AdapterRun]:
    platform = platform or collector.platform
    return [run_adapter(s, collector, store, target, salts=salts, platform=platform, params=(params or {}).get(s.id), approve=approve) for s in skills]


def record_denied(skill: Skill, store: Store, target: Target, reason: str) -> AdapterRun:
    """The human said no (or a guard refused): recorded like any other outcome, so coverage shows it."""
    import time
    run = AdapterRun(skill.id, skill.meta.ring, "denied", reason)
    return _finish(_Info(skill.id, skill.meta.ring), store, target, run, time.monotonic())


__all__ = ["AdapterRun", "all_adapters", "get_adapter", "library", "mode_of", "record_denied", "run_adapter", "survey"]
