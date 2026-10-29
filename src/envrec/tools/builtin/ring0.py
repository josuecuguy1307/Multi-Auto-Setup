"""The eleven Ring 0 probes as generic tools: what is this machine? Read-only, this machine only."""

from __future__ import annotations

from envrec.discovery.types import Capture
from envrec.probes import ring0
from envrec.tools.ctx import ToolContext, ToolResult
from envrec.tools.registry import tool


def _wrap(ctx: ToolContext, name: str, fn, takes_known: bool) -> ToolResult:
    out = fn(ctx.known()) if takes_known else fn()
    cap = Capture(name, f"probe:{name}", out.raw.encode("utf-8"), 0, "local", ctx.platform)
    return ToolResult(captures=[cap], facts=out.values, provider=out.provider, reasons=dict(out.reasons), limitations=list(out.limitations))


@tool(tags=("ring0", "probe", "generic_read"), kinds=("host",), produces=('os.name', 'os.version', 'os.arch', 'os.kernel'), requires=())
def read_os(ctx: ToolContext) -> ToolResult:
    """Read OS name, version, architecture and kernel with Python.

    Args:
        none
    Returns:
        fact keys: os.name, os.version, os.arch, os.kernel.
    Tier: observe
    Platforms: any
    Notes: Read OS name, version, architecture and kernel with Python. A value that could not be read comes back as None with a reason.
    """
    return _wrap(ctx, "read_os", ring0.read_os, False)


@tool(tags=("ring0", "probe", "generic_read"), kinds=("host",), produces=('os.name', 'os.version', 'os.arch', 'os.kernel'), requires=())
def read_os_native(ctx: ToolContext) -> ToolResult:
    """Cross-check the OS facts with the system's own commands.

    Args:
        none
    Returns:
        fact keys: os.name, os.version, os.arch, os.kernel.
    Tier: observe
    Platforms: any
    Notes: Cross-check the OS facts with the system's own commands. A value that could not be read comes back as None with a reason.
    """
    return _wrap(ctx, "read_os_native", ring0.read_os_native, False)


@tool(tags=("ring0", "probe", "generic_read"), kinds=("host",), produces=('hw.cpu_cores_physical', 'hw.cpu_cores_logical', 'hw.ram_total_gib'), requires=())
def read_cpu_ram(ctx: ToolContext) -> ToolResult:
    """Read core counts and total memory with psutil.

    Args:
        none
    Returns:
        fact keys: hw.cpu_cores_physical, hw.cpu_cores_logical, hw.ram_total_gib.
    Tier: observe
    Platforms: any
    Notes: Read core counts and total memory with psutil. A value that could not be read comes back as None with a reason.
    """
    return _wrap(ctx, "read_cpu_ram", ring0.read_cpu_ram, False)


@tool(tags=("ring0", "probe", "generic_read"), kinds=("host",), produces=('hw.cpu_model', 'hw.cpu_cores_physical', 'hw.cpu_cores_logical', 'hw.ram_total_gib'), requires=())
def read_hw_native(ctx: ToolContext) -> ToolResult:
    """Read CPU model and cross-check memory with system commands.

    Args:
        none
    Returns:
        fact keys: hw.cpu_model, hw.cpu_cores_physical, hw.cpu_cores_logical, hw.ram_total_gib.
    Tier: observe
    Platforms: any
    Notes: Read CPU model and cross-check memory with system commands. A value that could not be read comes back as None with a reason.
    """
    return _wrap(ctx, "read_hw_native", ring0.read_hw_native, False)


@tool(tags=("ring0", "probe", "generic_read"), kinds=("host",), produces=('hw.disk_total_gib', 'hw.disk_free_gib'), requires=())
def read_disks(ctx: ToolContext) -> ToolResult:
    """Read total and free space of the home disk.

    Args:
        none
    Returns:
        fact keys: hw.disk_total_gib, hw.disk_free_gib.
    Tier: observe
    Platforms: any
    Notes: Read total and free space of the home disk. A value that could not be read comes back as None with a reason.
    """
    return _wrap(ctx, "read_disks", ring0.read_disks, False)


@tool(tags=("ring0", "probe", "generic_read"), kinds=("host",), produces=('hw.battery_present',), requires=())
def read_battery(ctx: ToolContext) -> ToolResult:
    """Check whether a battery is present.

    Args:
        none
    Returns:
        fact keys: hw.battery_present.
    Tier: observe
    Platforms: any
    Notes: Check whether a battery is present. A value that could not be read comes back as None with a reason.
    """
    return _wrap(ctx, "read_battery", ring0.read_battery, False)


@tool(tags=("ring0", "probe", "generic_read"), kinds=("host",), produces=('hw.display_present',), requires=())
def read_display(ctx: ToolContext) -> ToolResult:
    """Check whether a display is attached.

    Args:
        none
    Returns:
        fact keys: hw.display_present.
    Tier: observe
    Platforms: any
    Notes: Check whether a display is attached. A value that could not be read comes back as None with a reason.
    """
    return _wrap(ctx, "read_display", ring0.read_display, False)


@tool(tags=("ring0", "probe", "generic_read"), kinds=("host",), produces=('host.virtualization',), requires=())
def read_virtualization(ctx: ToolContext) -> ToolResult:
    """Detect whether this runs in a container or virtual machine.

    Args:
        none
    Returns:
        fact keys: host.virtualization.
    Tier: observe
    Platforms: any
    Notes: Detect whether this runs in a container or virtual machine. A value that could not be read comes back as None with a reason.
    """
    return _wrap(ctx, "read_virtualization", ring0.read_virtualization, False)


@tool(tags=("ring0", "probe", "generic_read"), kinds=("host",), produces=('host.tools',), requires=())
def read_tool_versions(ctx: ToolContext) -> ToolResult:
    """List versions of common developer tools on this machine.

    Args:
        none
    Returns:
        fact keys: host.tools.
    Tier: observe
    Platforms: any
    Notes: List versions of common developer tools on this machine. A value that could not be read comes back as None with a reason.
    """
    return _wrap(ctx, "read_tool_versions", ring0.read_tool_versions, False)


@tool(tags=("ring0", "probe", "generic_read"), kinds=("host",), produces=('net.interfaces',), requires=())
def read_net_interfaces(ctx: ToolContext) -> ToolResult:
    """List this machine's own network interfaces that hold an address.

    Args:
        none
    Returns:
        fact keys: net.interfaces.
    Tier: observe
    Platforms: any
    Notes: List this machine's own network interfaces that hold an address. A value that could not be read comes back as None with a reason.
    """
    return _wrap(ctx, "read_net_interfaces", ring0.read_net_interfaces, False)


@tool(tags=("ring0", "probe", "generic_read"), kinds=("host",), produces=('host.kind', 'host.capabilities'), requires=('os.name', 'hw.battery_present', 'hw.display_present', 'host.virtualization', 'host.tools', 'net.interfaces'))
def classify_host(ctx: ToolContext) -> ToolResult:
    """Infer host kind and capabilities from the facts already known.

    Args:
        none
    Returns:
        fact keys: host.kind, host.capabilities.
    Tier: observe
    Platforms: any
    Notes: Infer host kind and capabilities from the facts already known. A value that could not be read comes back as None with a reason.
    """
    return _wrap(ctx, "classify_host", ring0.classify_host, True)

