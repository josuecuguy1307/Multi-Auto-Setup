"""The router (T4a): sensitivity in code, the written selection rule over a curated catalog, escalation, dispatch to environments. See docs/ROUTER_API.md."""

from envrec_coordinator.router.engine import Router
from envrec_coordinator.router.facts import DictFacts, MachineFacts, Policy
from envrec_coordinator.router.model import EntryChat, Exclude, GradeResult, Refusal, Route, RouteDecision, Task, TaskHandle
from envrec_coordinator.router.outputs import MemoryOutputs
from envrec_coordinator.router.sensitivity import creation_refusal, filter_outbound

__all__ = ["Router", "DictFacts", "MachineFacts", "Policy", "EntryChat", "Exclude", "GradeResult", "MemoryOutputs", "Refusal", "Route", "RouteDecision", "Task", "TaskHandle", "creation_refusal", "filter_outbound"]
