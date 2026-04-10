"""
BidirectionalCapabilityTester - safely probes the ECU to determine
whether a given bidirectional command's DID/RID is even recognised,
WITHOUT causing the actuator to move.

Probing strategy:
  * 0x22 ReadDataByIdentifier  -> send the DID, look for positive 0x62
                                  or NRC.  No side effects.
  * 0x2F InputOutputControl    -> send with control byte 0x00
                                  (ReturnControlToECU).  Per ISO 14229
                                  this explicitly returns the output
                                  to ECU management and will not move
                                  an actuator that isn't already being
                                  commanded by the tester.
  * 0x31 RoutineControl        -> send subfunction 0x03 (requestResults).
                                  A module that supports the routine
                                  will either return the last result or
                                  NRC 0x24 (requestSequenceError) - both
                                  of which confirm the routine exists.

The prober respects the SafetyGate and the per-command enabled/verified
flags just like the real controller.  On a real vehicle it will refuse
to transmit probes whose DID is unverified, so capability scanning on
an unknown truck runs entirely inside the simulator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..core.connection import OBDConnection, Response
from ..core.safety_gate import SafetyGate
from ..core.vehicle_matrix import BidirCommand, VehicleCompatibilityMatrix


@dataclass
class CommandProbeResult:
    command_name: str
    supported: bool
    nrc: Optional[int]
    probe_method: str
    notes: str


class BidirectionalCapabilityTester:
    def __init__(
        self,
        conn: OBDConnection,
        matrix: VehicleCompatibilityMatrix,
        safety_gate: SafetyGate,
    ) -> None:
        self.conn = conn
        self.matrix = matrix
        self.gate = safety_gate
        self._logger = logging.getLogger("CapabilityTester")

    # ------------------------------------------------------------------
    def probe_single(self, oem: str, cmd: BidirCommand) -> CommandProbeResult:
        # Hard safety checks first - mirror BidirectionalController.
        if cmd.service == 0x22 and not self.gate.allows_uds_read():
            return CommandProbeResult(
                cmd.name, False, None, "skip", self.gate.reason_blocked()
            )
        if cmd.service in (0x2F, 0x31) and not self.gate.allows_uds_actuator():
            return CommandProbeResult(
                cmd.name, False, None, "skip", self.gate.reason_blocked()
            )
        if not cmd.enabled or not cmd.verified:
            return CommandProbeResult(
                cmd.name,
                False,
                None,
                "skip",
                "command not enabled+verified in profile",
            )
        if not self.conn.is_alive():
            return CommandProbeResult(cmd.name, False, None, "none", "no connection")

        profile = self.matrix.get_profile(oem)
        if profile is None:
            return CommandProbeResult(cmd.name, False, None, "none", "no profile")
        module = profile.get_module(cmd.module)
        if module is None:
            return CommandProbeResult(
                cmd.name, False, None, "none", f"unknown module {cmd.module}"
            )

        # Build a read-only probe payload.
        if cmd.service == 0x22:
            payload = cmd.build_payload()
            method = "0x22 ReadDataByIdentifier"
        elif cmd.service == 0x2F:
            if cmd.did is None:
                return CommandProbeResult(cmd.name, False, None, "none", "no DID")
            payload = bytes(
                [0x2F, (cmd.did >> 8) & 0xFF, cmd.did & 0xFF, 0x00]
            )
            method = "0x2F ReturnControlToECU"
        elif cmd.service == 0x31:
            if cmd.rid is None:
                return CommandProbeResult(cmd.name, False, None, "none", "no RID")
            payload = bytes(
                [0x31, 0x03, (cmd.rid >> 8) & 0xFF, cmd.rid & 0xFF]
            )
            method = "0x31 requestRoutineResults"
        else:
            return CommandProbeResult(
                cmd.name, False, None, "none", f"unsupported service {hex(cmd.service)}"
            )

        header = f"{module.request_id:03X}"
        resp_filter = f"{module.response_id:03X}"

        if not self.conn.set_header(header) or not self.conn.set_response_filter(resp_filter):
            self.conn.reset_header()
            return CommandProbeResult(
                cmd.name, False, None, method, "header/filter set failed"
            )

        response: Response = self.conn.query(payload)
        self.conn.reset_header()

        return self._interpret(cmd, method, response, payload[0])

    # ------------------------------------------------------------------
    def _interpret(
        self,
        cmd: BidirCommand,
        method: str,
        response: Response,
        sid: int,
    ) -> CommandProbeResult:
        if response.error:
            return CommandProbeResult(
                cmd.name, False, None, method, f"adapter error: {response.error}"
            )
        if not response.lines:
            return CommandProbeResult(cmd.name, False, None, method, "no response")

        positive = f"{(sid + 0x40):02X}"
        negative = f"7F{sid:02X}"

        for line in response.lines:
            if negative in line:
                idx = line.find(negative)
                if len(line) >= idx + 6:
                    try:
                        nrc = int(line[idx + 4 : idx + 6], 16)
                    except ValueError:
                        continue
                    # 0x11/0x12/0x31 = the ECU doesn't know this
                    # service/subfunction/identifier -> unsupported.
                    if nrc in (0x11, 0x12, 0x31):
                        return CommandProbeResult(
                            cmd.name, False, nrc, method, "ECU rejects: not supported"
                        )
                    # Any other NRC means the ECU recognised the
                    # request but declined for situational reasons -
                    # that still counts as "supported".
                    return CommandProbeResult(
                        cmd.name, True, nrc, method, f"supported (NRC {hex(nrc)})"
                    )
            if positive in line:
                return CommandProbeResult(
                    cmd.name, True, None, method, "supported (positive)"
                )

        return CommandProbeResult(
            cmd.name, False, None, method, "no recognisable SID"
        )
