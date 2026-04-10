"""
BidirectionalController.

This module is the ONLY place in the codebase that transmits UDS
actuator or routine-control requests.  Every path through this module
passes through a SafetyGate check AND the per-command `enabled` /
`verified` flags loaded from the vehicle profile.

Framing is produced by ``BidirCommand.build_payload()``; this class
never assembles bytes on its own.  NRCs are extracted from the
line-based Response (one physical ELM327 line at a time), never by
substring-matching a concatenated hex dump.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from ..core.connection import OBDConnection, Response
from ..core.errors import SafetyInterlock, CommandNotSupportedError
from ..core.safety_gate import SafetyGate
from ..core.vehicle_matrix import (
    BidirCommand,
    SafetyLevel,
    VehicleCompatibilityMatrix,
)


@dataclass
class TestResult:
    command_name: str
    success: bool
    response_hex: str
    nrc: Optional[int]
    elapsed_ms: float
    notes: str = ""


# Human-readable NRCs we surface in the UI.
NRC_DESCRIPTIONS = {
    0x10: "general reject",
    0x11: "service not supported",
    0x12: "sub-function not supported",
    0x13: "incorrect message length",
    0x21: "busy, repeat request",
    0x22: "conditions not correct",
    0x24: "request sequence error",
    0x31: "request out of range",
    0x33: "security access denied",
    0x35: "invalid key",
    0x72: "general programming failure",
    0x7E: "sub-function not supported in active session",
    0x7F: "service not supported in active session",
}


class BidirectionalController:
    def __init__(
        self,
        conn: OBDConnection,
        matrix: VehicleCompatibilityMatrix,
        safety_gate: SafetyGate,
    ) -> None:
        self.conn = conn
        self.matrix = matrix
        self.gate = safety_gate
        self._logger = logging.getLogger("BidirectionalController")

    # ------------------------------------------------------------------
    def list_available_tests(self, oem: str) -> list[BidirCommand]:
        return self.matrix.get_bidir_commands(oem)

    # ------------------------------------------------------------------
    def _assert_runnable(self, cmd: BidirCommand) -> None:
        """Raise a clear SafetyInterlock if this command must not fire."""
        if not cmd.enabled:
            raise SafetyInterlock(
                f"{cmd.name}: command is disabled in the vehicle profile"
            )
        if not cmd.verified:
            raise SafetyInterlock(
                f"{cmd.name}: command DID/RID has not been verified "
                f"against an OEM service manual - refusing to transmit"
            )
        if cmd.service == 0x22:
            if not self.gate.allows_uds_read():
                raise SafetyInterlock(
                    f"{cmd.name}: UDS read is blocked. "
                    + self.gate.reason_blocked()
                )
        elif cmd.service == 0x2F:
            if not self.gate.allows_uds_actuator():
                raise SafetyInterlock(
                    f"{cmd.name}: UDS actuator control is blocked. "
                    + self.gate.reason_blocked()
                )
        elif cmd.service == 0x31:
            if not self.gate.allows_uds_routine():
                raise SafetyInterlock(
                    f"{cmd.name}: UDS routine control is blocked. "
                    + self.gate.reason_blocked()
                )
        else:
            raise SafetyInterlock(
                f"{cmd.name}: service {hex(cmd.service)} is not in the allow-list"
            )

    # ------------------------------------------------------------------
    def execute_test(
        self,
        oem: str,
        cmd: BidirCommand,
        confirm: bool = False,
    ) -> TestResult:
        """Run one command against the vehicle (or simulator).

        Flow:
          1. SafetyGate + per-command enabled/verified check.
          2. Confirmation check for CAUTION/DANGER levels.
          3. Connection + matrix validity.
          4. Set ATSH to the target module's request address and
             ATCRA to the module's response address.
          5. Send payload, read Response.
          6. Parse positive/negative response line-by-line.
          7. Unconditional safe-abort in ``finally`` (returnControlToECU
             for 0x2F, stopRoutine for 0x31).
          8. Restore broadcast header regardless of outcome.
        """
        self._logger.info(
            "execute_test requested: %s (service=%s module=%s safety=%s)",
            cmd.name,
            hex(cmd.service),
            cmd.module,
            cmd.safety_level.name,
        )

        self._assert_runnable(cmd)

        if cmd.safety_level in (SafetyLevel.CAUTION, SafetyLevel.DANGER) and not confirm:
            raise SafetyInterlock(
                f"{cmd.name} requires explicit confirmation "
                f"(safety level {cmd.safety_level.name})"
            )

        if not self.conn.is_alive():
            raise ConnectionError("OBD connection offline")

        profile = self.matrix.get_profile(oem)
        if profile is None:
            raise CommandNotSupportedError(f"no vehicle profile for {oem}")

        module = profile.get_module(cmd.module)
        if module is None:
            raise CommandNotSupportedError(
                f"{cmd.name}: module {cmd.module} not in profile {oem}"
            )

        # Build the payload exactly once, fail fast on bad data.
        try:
            payload = cmd.build_payload()
        except ValueError as exc:
            raise CommandNotSupportedError(str(exc)) from exc

        header = f"{module.request_id:03X}" if module.request_id <= 0xFFF else f"{module.request_id:08X}"
        resp_filter = f"{module.response_id:03X}" if module.response_id <= 0xFFF else f"{module.response_id:08X}"

        start = time.monotonic()
        response: Optional[Response] = None
        success = False
        nrc: Optional[int] = None
        notes = ""

        try:
            if not self.conn.set_header(header):
                raise ConnectionError(f"failed to set ATSH {header}")
            if not self.conn.set_response_filter(resp_filter):
                raise ConnectionError(f"failed to set ATCRA {resp_filter}")

            response = self.conn.query(payload)
            success, nrc, notes = self._interpret_response(cmd.service, response)
        finally:
            # SAFETY: unconditionally try to return ECU control to a
            # passive state before releasing the bus.
            abort = cmd.abort_payload()
            if abort is not None:
                try:
                    self.conn.query(abort)
                except Exception as exc:
                    self._logger.error(
                        "abort payload failed after %s: %s", cmd.name, exc
                    )
            # Always return the adapter to the OBD-II broadcast header.
            try:
                self.conn.reset_header()
            except Exception:
                pass

        elapsed_ms = (time.monotonic() - start) * 1000.0
        return TestResult(
            command_name=cmd.name,
            success=success,
            response_hex=response.joined_hex() if response else "",
            nrc=nrc,
            elapsed_ms=elapsed_ms,
            notes=notes,
        )

    # ------------------------------------------------------------------
    def _interpret_response(
        self, sid: int, response: Response
    ) -> tuple[bool, Optional[int], str]:
        """Walk the response lines and return (success, nrc, notes)."""
        if response.error:
            return False, None, f"adapter error: {response.error}"
        if not response.lines:
            return False, None, "empty response"

        positive_sid = f"{(sid + 0x40):02X}"
        negative_tag = f"7F{sid:02X}"

        for line in response.lines:
            # A real ELM327 response line will begin with the 3-hex
            # CAN header (e.g. "7E8") then an ISO-TP first byte with a
            # length nibble then the UDS payload.  We search for our
            # SID anywhere inside that line; the positive/negative
            # form is unambiguous.
            if negative_tag in line:
                idx = line.find(negative_tag)
                if len(line) >= idx + 6:
                    try:
                        nrc = int(line[idx + 4 : idx + 6], 16)
                    except ValueError:
                        continue
                    desc = NRC_DESCRIPTIONS.get(nrc, "unknown NRC")
                    return False, nrc, desc
            if positive_sid in line:
                return True, None, "positive response"

        return False, None, "no recognisable SID in response"

    # ------------------------------------------------------------------
    def abort_test(self, cmd: Optional[BidirCommand] = None) -> bool:
        """Operator-initiated abort.

        For 0x2F: send ReturnControlToECU for the active DID.
        For 0x31: send stopRoutine for the active RID.
        Always ends by restoring the broadcast header.
        """
        if not self.conn.is_alive():
            return False
        ok = True
        try:
            if cmd is not None:
                abort = cmd.abort_payload()
                if abort is not None:
                    r = self.conn.query(abort)
                    if r.error:
                        ok = False
        finally:
            try:
                self.conn.reset_header()
            except Exception:
                ok = False
        return ok
