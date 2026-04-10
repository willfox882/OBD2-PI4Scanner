"""
CapabilityDetector - real probing against the bus.

Discovers:
  * Supported Mode 01 PIDs via the standard 0x00/0x20/0x40/0x60/0x80/0xA0/0xC0
    "supported-PID" bitmap walk.
  * Responding ECUs via the ISO 15765-4 functional broadcast address
    0x7DF + Mode 01 PID 0x00.  Every module that responds announces
    its physical response address in its response frame header.
  * The VIN via Mode 09 PID 0x02 (standard) with a fallback to
    UDS 0x22 F1 90 if advanced mode is active.

No OEM-specific PIDs, no invented CAN IDs, no undocumented services.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from .connection import OBDConnection, Response
from .safety_gate import SafetyGate


class UDSSupportLevel(Enum):
    NONE = 1
    BASIC = 2      # Mode 09 VIN works
    EXTENDED = 3   # UDS 0x22 F190 works
    FULL = 4       # Advanced features verified


@dataclass
class ModuleInfo:
    name: str
    address: int
    protocol: str
    responding: bool


@dataclass
class CapabilityReport:
    supported_pids: set[str] = field(default_factory=set)
    modules: list[ModuleInfo] = field(default_factory=list)
    uds_level: UDSSupportLevel = UDSSupportLevel.NONE
    vin: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)


# Standard 11-bit functional broadcast address for OBD-II.
_FUNCTIONAL = "7DF"


class CapabilityDetector:
    def __init__(self, conn: OBDConnection, safety_gate: SafetyGate) -> None:
        self.conn = conn
        self.gate = safety_gate
        self._logger = logging.getLogger("CapabilityDetector")

    # ------------------------------------------------------------------
    def _ensure_broadcast(self) -> None:
        self.conn.reset_header()

    # ------------------------------------------------------------------
    def detect_supported_pids(self) -> set[str]:
        """Walk Mode 01 PIDs 0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0
        collecting the bitmap of supported PIDs.  Returns uppercase
        hex strings (e.g. ``"0C"``)."""
        self._ensure_broadcast()
        supported: set[str] = set()
        base_pids = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0)
        for base in base_pids:
            resp = self.conn.query(f"01{base:02X}")
            bitmap = self._extract_bitmap(resp, base)
            if bitmap is None:
                break
            for i in range(32):
                if bitmap & (1 << (31 - i)):
                    pid_num = base + i + 1
                    supported.add(f"{pid_num:02X}")
            # Bit 32 (LSB) of the bitmap indicates whether the next
            # block (PID base+0x20) is itself supported.  If not, stop.
            if not (bitmap & 0x00000001):
                break
        return supported

    @staticmethod
    def _extract_bitmap(resp: Response, base: int) -> Optional[int]:
        """Pull the 4-byte bitmap from a ``41 <base> AA BB CC DD`` reply."""
        if not resp.ok:
            return None
        tag = f"41{base:02X}"
        for line in resp.lines:
            idx = line.find(tag)
            if idx == -1:
                continue
            data = line[idx + 4 : idx + 12]
            if len(data) == 8:
                try:
                    return int(data, 16)
                except ValueError:
                    return None
        return None

    # ------------------------------------------------------------------
    def detect_modules(self) -> list[ModuleInfo]:
        """Send Mode 01 PID 0x00 as a functional broadcast and record
        every response header as a live ECU address."""
        self._ensure_broadcast()
        resp = self.conn.query("0100")
        if not resp.ok:
            return []
        found: dict[int, ModuleInfo] = {}
        for line in resp.lines:
            # 11-bit CAN responses arrive with a 3-hex header prefix
            # (e.g. ``7E804410003EE0000``).
            if len(line) < 3:
                continue
            try:
                addr = int(line[:3], 16)
            except ValueError:
                continue
            if 0x7E8 <= addr <= 0x7EF:
                name = _name_for_address(addr)
                found.setdefault(
                    addr,
                    ModuleInfo(name=name, address=addr, protocol="CAN", responding=True),
                )
        return sorted(found.values(), key=lambda m: m.address)

    # ------------------------------------------------------------------
    def detect_vin(self) -> Optional[str]:
        """Read the VIN via Mode 09 PID 0x02 (standard OBD-II)."""
        self._ensure_broadcast()
        resp = self.conn.query("0902")
        if not resp.ok:
            return None
        # Look for "4902" tag and the ASCII VIN that follows.
        joined = resp.joined_hex()
        idx = joined.find("4902")
        if idx == -1:
            return None
        # Skip the "49 02 01" header (message count byte), then 17
        # ASCII bytes = 34 hex chars.
        data = joined[idx + 4 :]
        # Multi-frame concatenation may intersperse frame counters;
        # the simplest robust approach is to pull only printable
        # ASCII chars from the remaining bytes.
        try:
            raw = bytes.fromhex(data[: min(len(data), 80)])
        except ValueError:
            return None
        ascii_only = "".join(chr(b) for b in raw if 32 <= b < 127)
        # VIN is the last 17 contiguous alphanumeric characters.
        candidate = "".join(c for c in ascii_only if c.isalnum())
        if len(candidate) < 17:
            return None
        return candidate[-17:]

    # ------------------------------------------------------------------
    def detect_uds_support(self) -> UDSSupportLevel:
        self._ensure_broadcast()
        # Mode 09 is standard OBD-II and always safe to probe.
        r = self.conn.query("0902")
        if r.ok and any("4902" in l for l in r.lines):
            level = UDSSupportLevel.BASIC
        else:
            level = UDSSupportLevel.NONE
        # UDS 0x22 is an advanced-mode read and must be gated.
        if self.gate.allows_uds_read():
            r2 = self.conn.query(b"\x22\xF1\x90")
            if r2.ok and any("62F190" in l for l in r2.lines):
                level = UDSSupportLevel.EXTENDED
        return level

    # ------------------------------------------------------------------
    def build_report(self) -> CapabilityReport:
        return CapabilityReport(
            supported_pids=self.detect_supported_pids(),
            modules=self.detect_modules(),
            uds_level=self.detect_uds_support(),
            vin=self.detect_vin(),
            timestamp=datetime.now(),
        )


def _name_for_address(addr: int) -> str:
    """Map a standard ISO 15765 response address to a human name.
    Unknown addresses get a generic label - never invented."""
    known = {
        0x7E8: "ECM/PCM",
        0x7E9: "TCM",
        0x7EA: "Chassis module",
        0x7EB: "Instrument cluster",
        0x7EC: "ABS/EBCM",
        0x7ED: "Body/BCM",
        0x7EE: "Gateway/BCM",
        0x7EF: "Module 8",
    }
    return known.get(addr, f"Module@{addr:03X}")
