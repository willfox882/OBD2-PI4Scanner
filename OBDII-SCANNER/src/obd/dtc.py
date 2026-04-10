"""
DTCManager - standard OBD-II Mode 03 / 04 / 07 / 0A.

All reads and clears use ONLY the standardised OBD-II services.  No
UDS 0x14 / 0x19 calls - the original code mixed those in, but they are
out of scope for the universal safe mode and add nothing that Mode
03/04/07/0A cannot do on an ISO 15765-4 vehicle.

Response parsing walks the structured Response.lines list instead of
substring-matching a concatenated hex dump.  This is the only way to
correctly tolerate multi-ECU and multi-frame replies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from ..core.connection import OBDConnection, Response


class DTCStatus(Enum):
    STORED = 1
    PENDING = 2
    PERMANENT = 3


@dataclass
class DTCEntry:
    code: str
    description: str
    status: DTCStatus
    module: str
    freeze_frame: Optional[dict]
    timestamp: datetime


_PREFIX = {
    0: "P0", 1: "P1", 2: "P2", 3: "P3",
    4: "C0", 5: "C1", 6: "C2", 7: "C3",
    8: "B0", 9: "B1", 10: "B2", 11: "B3",
    12: "U0", 13: "U1", 14: "U2", 15: "U3",
}


class DTCManager:
    def __init__(self, conn: OBDConnection) -> None:
        self.conn = conn
        self._logger = logging.getLogger("DTCManager")

    # ------------------------------------------------------------------
    def read_stored(self) -> list[DTCEntry]:
        return self._read("03", 0x43, DTCStatus.STORED)

    def read_pending(self) -> list[DTCEntry]:
        return self._read("07", 0x47, DTCStatus.PENDING)

    def read_permanent(self) -> list[DTCEntry]:
        return self._read("0A", 0x4A, DTCStatus.PERMANENT)

    def clear_codes(self) -> bool:
        """Mode 04 - Clear Emissions-Related Diagnostic Information.

        Per SAE J1979 the ECU responds with ``44`` on success.  Any
        other outcome is treated as failure so the UI can surface it.
        """
        self._logger.warning("Clearing DTCs (Mode 04)")
        resp = self.conn.query("04")
        if not resp.ok:
            return False
        return any("44" in line for line in resp.lines)

    # ------------------------------------------------------------------
    def _read(self, cmd: str, pos_tag: int, status: DTCStatus) -> list[DTCEntry]:
        resp = self.conn.query(cmd)
        return self._parse(resp, pos_tag, status)

    def _parse(self, resp: Response, pos_tag: int, status: DTCStatus) -> list[DTCEntry]:
        if not resp.ok:
            return []
        tag_hex = f"{pos_tag:02X}"
        seen: set[str] = set()
        dtcs: list[DTCEntry] = []
        for line in resp.lines:
            idx = line.find(tag_hex)
            if idx == -1:
                continue
            data = line[idx + 2 :]
            # Mode 03/07/0A format (CAN): count byte followed by N*2 DTC bytes.
            # Some ECUs omit the count byte; try both interpretations.
            for start in (2, 0):
                parsed = self._scan_dtcs(data[start:], status)
                if parsed:
                    for d in parsed:
                        if d.code not in seen:
                            seen.add(d.code)
                            dtcs.append(d)
                    break
        return dtcs

    def _scan_dtcs(self, data_hex: str, status: DTCStatus) -> list[DTCEntry]:
        out: list[DTCEntry] = []
        for i in range(0, len(data_hex) - 3, 4):
            chunk = data_hex[i : i + 4]
            if chunk == "0000":
                continue
            try:
                entry = self._decode(chunk, status)
            except ValueError:
                continue
            out.append(entry)
        return out

    @staticmethod
    def _decode(raw_hex: str, status: DTCStatus) -> DTCEntry:
        b1 = int(raw_hex[0:2], 16)
        prefix = _PREFIX[b1 >> 4]
        digit2 = f"{b1 & 0x0F:X}"
        tail = raw_hex[2:4].upper()
        code = f"{prefix}{digit2}{tail}"
        return DTCEntry(
            code=code,
            description=DTCManager._describe(code),
            status=status,
            module="Generic",
            freeze_frame=None,
            timestamp=datetime.now(),
        )

    @staticmethod
    def _describe(code: str) -> str:
        common = {
            "P0171": "System Too Lean (Bank 1)",
            "P0300": "Random/Multiple Cylinder Misfire Detected",
            "P0420": "Catalyst System Efficiency Below Threshold",
            "P0455": "Evaporative Emission System Leak Detected (large)",
            "P0500": "Vehicle Speed Sensor Malfunction",
            "U0100": "Lost Communication with ECM/PCM",
        }
        return common.get(code, "Manufacturer specific or unknown code")
