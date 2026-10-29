"""Shared parsers for tool modules: the ARP / neighbor table."""

from __future__ import annotations

import ipaddress
import re


def neighbor_rows(text: str, platform: str) -> tuple[list[dict], int]:
    rows, incomplete = [], 0
    for ln in text.splitlines():
        if re.search(r"\bpermanent\b|\bPERMANENT\b", ln):
            continue  # this machine's own address, not a neighbor
        if platform == "macos":
            m = re.match(r"\S+ \((\d+\.\d+\.\d+\.\d+)\) at (\S+) on (\S+)", ln)
            if not m:
                continue
            ip, mac, iface = m.groups()
            if mac == "(incomplete)":
                incomplete += 1
                continue
        else:
            m = re.match(r"(\d+\.\d+\.\d+\.\d+) dev (\S+) lladdr (\S+)", ln)
            if not m:
                if re.match(r"\d+\.\d+\.\d+\.\d+ dev \S+ .*(FAILED|INCOMPLETE)", ln):
                    incomplete += 1
                continue
            ip, iface, mac = m.groups()
        mac = ":".join(p.zfill(2) for p in mac.lower().split(":"))
        try:
            addr = ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        if addr.is_multicast or mac == "ff:ff:ff:ff:ff:ff" or ip.endswith(".255"):
            continue
        rows.append({"ip": ip, "mac": mac, "iface": iface})
    return rows, incomplete
