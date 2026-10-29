"""Serial protocol signatures, written for this project: NMEA, SCPI *IDN?, AT, Modbus RTU/ASCII, G-code.

A signature match is a hypothesis about the protocol, never proof: several of these can match
the same bytes, so every match carries a confidence and the line that caused it. Checksums
(NMEA XOR, Modbus CRC16/LRC) are verified, not just pattern-matched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from envrec.fingerprint.base import Candidate

SOURCE = "serial.signatures"
DATA_VERSION = "builtin-v1"

_NMEA = re.compile(r"^\$([A-Z]{2}[A-Z0-9]{3}),([^*]*)\*([0-9A-Fa-f]{2})$")
_AT_REPLY = re.compile(r"^(OK|ERROR|\+CM[ES] ERROR:.*|\+[A-Z]{2,}[A-Z0-9]*:.*|NO CARRIER|BUSY|RING)$")
_GRBL = re.compile(r"^Grbl\s+\d+(\.\d+)*\S*", re.I)
_GCODE_FIRMWARE = re.compile(r"^(echo:\s*)?(Marlin|Klipper|RepRap|Smoothie)\b", re.I)
_GCODE_CMD = re.compile(r"^(G\d{1,3}|M\d{1,3}|\$[A-Z0-9$#]*)\b", re.I)


@dataclass(frozen=True)
class SerialMatch:
    protocol: str  # nmea | scpi | at | modbus | gcode
    confidence: float
    evidence: str  # the line or frame that matched, shortened

    def as_candidate(self) -> Candidate:
        return Candidate(SOURCE, self.protocol, f"{self.protocol}: {self.evidence}", DATA_VERSION, "protocol")


def nmea_checksum_ok(line: str) -> bool:
    m = _NMEA.match(line)
    if not m:
        return False
    body = line[1 : line.index("*")]
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    return calc == int(m.group(3), 16)


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def modbus_rtu_ok(frame: bytes) -> bool:
    """Valid CRC16 (low byte first), address 1-247 and a plausible function or exception code."""
    if len(frame) < 4 or not 1 <= frame[0] <= 247:
        return False
    fn = frame[1] & 0x7F
    if fn not in (1, 2, 3, 4, 5, 6, 15, 16, 22, 23, 43):
        return False
    return crc16_modbus(frame[:-2]) == int.from_bytes(frame[-2:], "little")


def modbus_ascii_ok(line: str) -> bool:
    if not line.startswith(":") or len(line) < 9 or (len(line) - 1) % 2:
        return False
    try:
        raw = bytes.fromhex(line[1:])
    except ValueError:
        return False
    return (-sum(raw[:-1])) & 0xFF == raw[-1]


def _scpi_idn(line: str) -> bool:
    parts = [p.strip() for p in line.split(",")]
    return len(parts) in (4, 5) and bool(parts[0]) and bool(parts[1]) and any(c.isalpha() for c in parts[0])


def match_serial(sample: bytes | str, sent: str | None = None) -> list[SerialMatch]:
    """Which protocols could have produced this reply, best first.

    `sent` is what we sent before reading (for example '*IDN?'); it raises confidence for
    protocols that are only meaningful as a reply to a known command.
    """
    matches: list[SerialMatch] = []
    sent_u = (sent or "").strip().upper()

    if isinstance(sample, bytes):
        if modbus_rtu_ok(sample):
            matches.append(SerialMatch("modbus", 0.85, f"RTU frame, valid CRC16, {len(sample)} bytes"))
        text = sample.decode("ascii", errors="replace")
    else:
        text = sample
    lines = [ln.strip() for ln in text.replace("\r", "\n").split("\n") if ln.strip()]

    for ln in lines:
        short = ln[:60]
        if _NMEA.match(ln):
            ok = nmea_checksum_ok(ln)
            matches.append(SerialMatch("nmea", 0.95 if ok else 0.5, f"{short} (checksum {'ok' if ok else 'BAD'})"))
        elif modbus_ascii_ok(ln):
            matches.append(SerialMatch("modbus", 0.85, f"ASCII frame, valid LRC: {short}"))
        elif _scpi_idn(ln):
            matches.append(SerialMatch("scpi", 0.9 if sent_u == "*IDN?" else 0.4, f"4-field identity reply: {short}"))
        elif _GRBL.match(ln) or _GCODE_FIRMWARE.match(ln):
            matches.append(SerialMatch("gcode", 0.95, f"firmware banner: {short}"))
        elif _AT_REPLY.match(ln):
            if sent_u.startswith("AT"):
                matches.append(SerialMatch("at", 0.9, f"reply to {sent_u}: {short}"))
            elif ln.startswith("+") or ln.endswith("ERROR"):
                matches.append(SerialMatch("at", 0.5, f"AT-style reply: {short}"))
            elif ln == "OK" and _GCODE_CMD.match(sent_u):
                matches.append(SerialMatch("gcode", 0.6, f"'ok' after G-code command {sent_u}"))
            elif ln == "OK":
                matches.append(SerialMatch("at", 0.2, "bare 'OK' (also what many protocols say)"))
        elif ln.lower() == "ok" and _GCODE_CMD.match(sent_u):
            matches.append(SerialMatch("gcode", 0.6, f"'ok' after G-code command {sent_u}"))

    best: dict[str, SerialMatch] = {}
    for m in matches:
        if m.protocol not in best or m.confidence > best[m.protocol].confidence:
            best[m.protocol] = m
    return sorted(best.values(), key=lambda m: -m.confidence)
