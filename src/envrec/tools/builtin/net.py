"""The network layer's hands as typed tools (docs/NETWORK_CONTRACT.md): read this machine's node card, send a heartbeat to the coordinator, list the paired nodes. Strict arguments,
evidence-tagged results. The same functions the node agent and the coordinator CLI use; a model-driven run reaches them through the menu like any other tool."""

from __future__ import annotations

import time

from envrec.discovery.types import Reason
from envrec.tools.ctx import ToolContext, ToolResult
from envrec.tools.registry import tool

ONLINE_WINDOW_S = 30.0


@tool(tags=("net", "node", "passive"), kinds=("node",), ring=1, produces=("node.hostname", "node.os", "node.arch"))
def node_card_read(ctx: ToolContext) -> ToolResult:
    """Read this machine's node card basics.

    Args:
        none
    Returns:
        Facts node.hostname, node.os (darwin, linux or windows) and node.arch.
    Tier: observe
    Platforms: any
    Notes: host name, operating system family and CPU architecture only; read through the per-OS layer, nothing is executed and nothing is sent.
    """
    import envrec_os
    c = envrec_os.card()
    return ToolResult(facts={"node.hostname": c["hostname"], "node.os": c["os"], "node.arch": c["arch"]}, provider="envrec_os")


def _heartbeat_text(ctx: ToolContext) -> str:
    return "send one heartbeat to the coordinator over this node's own pinned channel: host name, operating system, architecture and a counter, signed with this node's key (nothing else)"


@tool(tags=("net", "node", "active"), kinds=("node",), ring=1, produces=("net.heartbeat_acked",), timeout_s=30, approval=_heartbeat_text)
def node_heartbeat_send(ctx: ToolContext) -> ToolResult:
    """Send one heartbeat to the coordinator now.

    Args:
        none
    Returns:
        Fact net.heartbeat_acked: true only when the coordinator's signed acknowledgement arrived.
    Tier: observe
    Platforms: any
    Notes: sends the signed card basics over the node's own pinned channel (it dials out; nothing listens); needs the running node agent in the run's parameters.
    """
    link = ctx.params.get("node_link")
    if link is None:
        return ToolResult.fail(Reason.not_applicable, "no node agent is connected in this run")
    res = link.heartbeat_now()
    return ToolResult(facts={"net.heartbeat_acked": bool(res.get("acked"))}, provider="mas-node", limitations=[] if res.get("acked") else [str(res.get("reason", "no acknowledgement"))[:100]])


@tool(tags=("net", "coordinator", "passive"), kinds=("pairing", "node"), ring=1, produces=("net.nodes_count", "net.nodes_online"))
def nodes_list(ctx: ToolContext) -> ToolResult:
    """Count the paired nodes and how many sent a heartbeat lately.

    Args:
        none
    Returns:
        Facts net.nodes_count and net.nodes_online (a heartbeat in the last 30 seconds).
    Tier: observe
    Platforms: any
    Notes: reads the coordinator's registry of paired nodes; names and keys are not returned as facts.
    """
    reg = ctx.params.get("coordinator_registry")
    if reg is None:
        return ToolResult.fail(Reason.not_applicable, "no coordinator registry in this run")
    now = float(ctx.params.get("clock", time.time)())
    nodes = reg.all()
    return ToolResult(facts={"net.nodes_count": len(nodes), "net.nodes_online": sum(1 for n in nodes if n.last_heartbeat and now - n.last_heartbeat <= ONLINE_WINDOW_S)}, provider="mas-coordinator")
