# Playbook: host (this machine)

Goal: find out what machine you run on. Facts first, then one judgement.

1. Read the basics: `read_os`, `read_cpu_ram`, `read_disks`.
2. Cross-check anything that matters with the native tools (`read_os_native`, `read_hw_native`). Two sources that agree are worth more than one.
3. Then the context probes: `read_battery`, `read_display`, `read_virtualization`, `read_tool_versions`, `read_net_interfaces`.
4. Last, `classify_host`. It needs the context probes done first.
5. A fact marked unknown means the probe tried and could not tell. Do not call that probe again.
6. Reply `done` when the menu has nothing useful left.

Pick tools only from the MENU. One JSON object per reply.
