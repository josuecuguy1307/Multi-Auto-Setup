"""Facts that change on their own. Scoring uses a tolerance or reports them apart; they are never judged false-tested."""

from __future__ import annotations

import fnmatch

VOLATILE_PATTERNS = (
    "hw.disk_free_gib", "hw.volume.*.free_gib", "os.uptime_s", "os.load_avg", "net.wifi.rssi", "net.wifi.noise", "net.wifi.tx_rate",
    "net.wifi.nearby_count", "net.listening.*", "app.process_count", "app.process_classes", "hw.battery_percent", "hw.battery_charging",
    "hw.thermal_state", "hw.memory_free_gib", "net.neighbors_count", "net.lan_hosts_count", "ble.nearby_*", "ble.connected_count", "usb.device_count", "serial.port_count",
)


def is_volatile(key: str) -> bool:
    return any(fnmatch.fnmatchcase(key, p) for p in VOLATILE_PATTERNS)
