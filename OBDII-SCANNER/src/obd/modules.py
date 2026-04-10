"""
ModuleScanner - discovers responsive ECUs on the CAN bus.

Uses the standard ISO 15765-4 functional broadcast address 0x7DF with
Mode 01 PID 0x00 to elicit a response from every OBD-compliant module.
For module-specific DTC reads it temporarily retargets ATSH to the
module's request address and ATCRA to its response address before
delegating to DTCManager.  The broadcast header is always restored.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..core.capability_detection import CapabilityDetector, ModuleInfo
from ..core.connection import OBDConnection
from ..core.safety_gate import SafetyGate
from ..core.vehicle_matrix import VehicleCompatibilityMatrix
from .dtc import DTCEntry, DTCManager


class ModuleScanner:
    def __init__(
        self,
        conn: OBDConnection,
        matrix: VehicleCompatibilityMatrix,
        safety_gate: SafetyGate,
    ) -> None:
        self.conn = conn
        self.matrix = matrix
        self.gate = safety_gate
        self._detector = CapabilityDetector(conn, safety_gate)
        self._logger = logging.getLogger("ModuleScanner")

    # ------------------------------------------------------------------
    def scan_all(self) -> list[ModuleInfo]:
        if not self.conn.is_alive():
            return []
        return self._detector.detect_modules()

    # ------------------------------------------------------------------
    def scan_module(self, address: int) -> Optional[ModuleInfo]:
        """Test a specific module address by setting the response
        filter and sending Mode 01 PID 0x00."""
        if not self.conn.is_alive():
            return None
        req_addr = address - 0x08 if 0x7E8 <= address <= 0x7EF else address
        try:
            if not self.conn.set_header(f"{req_addr:03X}"):
                return None
            if not self.conn.set_response_filter(f"{address:03X}"):
                return None
            resp = self.conn.query("0100")
            if resp.ok and resp.lines:
                return ModuleInfo(
                    name=_name_for_address(address),
                    address=address,
                    protocol="CAN",
                    responding=True,
                )
            return None
        finally:
            self.conn.reset_header()

    # ------------------------------------------------------------------
    def get_module_dtcs(self, address: int) -> list[DTCEntry]:
        if not self.conn.is_alive():
            return []
        req_addr = address - 0x08 if 0x7E8 <= address <= 0x7EF else address
        try:
            if not self.conn.set_header(f"{req_addr:03X}"):
                return []
            if not self.conn.set_response_filter(f"{address:03X}"):
                return []
            dtc_mgr = DTCManager(self.conn)
            return dtc_mgr.read_stored()
        finally:
            self.conn.reset_header()


def _name_for_address(addr: int) -> str:
    return {
        0x7E8: "ECM/PCM",
        0x7E9: "TCM",
        0x7EA: "Chassis",
        0x7EB: "IPC",
        0x7EC: "ABS/EBCM",
        0x7ED: "BCM",
        0x7EE: "Gateway",
        0x7EF: "Aux",
    }.get(addr, f"Module@{addr:03X}")
