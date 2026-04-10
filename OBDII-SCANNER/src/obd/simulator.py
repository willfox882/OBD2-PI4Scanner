"""
MockOBDConnection - a drop-in stand-in for src.core.connection.OBDConnection
that does not touch any serial hardware.  Activated when the environment
variable ``OBD2_SIMULATOR=1`` is set.

Scope:
  * Mimics the public interface of OBDConnection (connect, disconnect,
    reconnect, is_alive, query, set_header, set_response_filter,
    reset_header, current_header, voltage, protocol_name, port_name).
  * Responds to a curated subset of Mode 01, 03, 04, 07, 09 requests.
  * Responds to UDS 0x22 / 0x2F / 0x31 against a simulated 2015 GMC
    Sierra 1500 4.3L LV3 PCM / TCM / BCM / EBCM / IPC.
  * Returns well-formed Response objects that exercise the same
    line-by-line parser the real adapter uses.

The simulator intentionally reports a VIN that WILL match the GMC
Sierra 4.3L LV3 profile so developers can exercise advanced-mode code
paths end-to-end without hardware.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Optional, Union

from ..core.connection import ConnectionState, Response

# Deterministic valid-for-2015-GMC-Sierra VIN:
#   3GT = GMC light-duty truck (Mexico-built)
#   position 8 = 'H' -> 4.3L LV3
#   position 10 = 'F' -> 2015
# Built to satisfy src.core.safety_gate.decode_vin():
#   pos 1-3 = 3GT (GMC light-duty WMI)
#   pos 8   = H   (4.3L LV3 V6 engine code)
#   pos 10  = F   (2015 model year)
_SIM_VIN = "3GTU2UEH5FG123456"


class _SimECU:
    """A single simulated ECU with its own DID + RID tables."""

    def __init__(self, name: str, request_id: int, response_id: int) -> None:
        self.name = name
        self.request_id = request_id
        self.response_id = response_id
        # Map DID -> bytes (for 0x22 read) or None (unsupported -> NRC 0x31)
        self.did_read: dict[int, bytes] = {}
        # Map DID -> allowed control parameters for 0x2F
        self.did_control: dict[int, set[int]] = {}
        # Map RID -> set of allowed subfunctions for 0x31
        self.rid_routines: dict[int, set[int]] = {}
        # Tracks which DIDs are currently "under tester control" so
        # ReturnControlToECU can flip them back.
        self.controlled_dids: set[int] = set()


def _build_sim_fleet() -> dict[int, _SimECU]:
    """Build the virtual bus topology for the 2015 Sierra 4.3L LV3."""
    pcm = _SimECU("PCM", 0x7E0, 0x7E8)
    pcm.did_read[0xF190] = _SIM_VIN.encode("ascii")
    pcm.did_read[0xF188] = b"12679604"  # placeholder SW part number
    # Accept shortTermAdjustment control for a couple of DIDs so the
    # sandbox can exercise 0x2F positive + NRC + return-control flows.
    pcm.did_control[0xF010] = {0x00, 0x01, 0x02, 0x03}
    pcm.did_control[0xF011] = {0x00, 0x03}
    # Routine 0x0200 = simulated EVAP leak test
    pcm.rid_routines[0x0200] = {0x01, 0x02, 0x03}

    tcm = _SimECU("TCM", 0x7E1, 0x7E9)
    tcm.did_read[0xF190] = _SIM_VIN.encode("ascii")

    ebcm = _SimECU("EBCM", 0x7E4, 0x7EC)
    bcm = _SimECU("BCM", 0x7E6, 0x7EE)
    ipc = _SimECU("IPC", 0x7E3, 0x7EB)

    fleet: dict[int, _SimECU] = {}
    for ecu in (pcm, tcm, ebcm, bcm, ipc):
        fleet[ecu.request_id] = ecu
    return fleet


class MockOBDConnection:
    """Simulated OBD-II / UDS adapter."""

    def __init__(self, port: str = "sim://gmc-sierra", baudrate: int = 0, timeout: float = 0.0):
        self.port_name = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.state = ConnectionState.DISCONNECTED
        self.voltage = 14.2
        self.protocol_name = "ISO 15765-4 (CAN 11/500)"

        self._logger = logging.getLogger("MockOBDConnection")
        self._lock = threading.RLock()
        self._current_header: Optional[str] = "7DF"
        self._current_filter: Optional[str] = None
        self._fleet = _build_sim_fleet()

        # Simulated live data.
        self._rpm = 720
        self._speed = 0
        self._coolant = 85
        self._last_tick = time.monotonic()

        # A small fixed set of DTCs the "truck" will report.
        self._stored_dtcs = [0x0420, 0x0171]  # P0420, P0171

    # ------------------------------------------------------------------
    def connect(self) -> bool:
        with self._lock:
            self.state = ConnectionState.CONNECTED
            self._logger.info("Mock connection established")
            return True

    def disconnect(self) -> None:
        with self._lock:
            self.state = ConnectionState.DISCONNECTED

    def reconnect(self, attempts: int = 3, delay: float = 0.0) -> bool:
        return self.connect()

    def is_alive(self) -> bool:
        return self.state == ConnectionState.CONNECTED

    @property
    def current_header(self) -> Optional[str]:
        return self._current_header

    def set_header(self, header: str) -> bool:
        self._current_header = header.upper().strip()
        return True

    def set_response_filter(self, addr: Optional[str]) -> bool:
        self._current_filter = addr.upper().strip() if addr else None
        return True

    def reset_header(self) -> bool:
        self._current_header = "7DF"
        self._current_filter = None
        return True

    # ------------------------------------------------------------------
    def query(self, command: Union[str, bytes]) -> Response:
        with self._lock:
            if not self.is_alive():
                return Response(error="DISCONNECTED")

            if isinstance(command, bytes):
                hex_cmd = command.hex().upper()
            else:
                hex_cmd = command.strip().upper()

            # AT commands -> OK
            if hex_cmd.startswith("AT"):
                return Response(lines=["OK"], error=None)

            try:
                payload = bytes.fromhex(hex_cmd)
            except ValueError:
                return Response(error="?")

            if not payload:
                return Response(error="EMPTY")

            service = payload[0]

            # Which ECU is being addressed?
            header = self._current_header or "7DF"
            try:
                target_req = int(header, 16)
            except ValueError:
                target_req = 0x7DF

            broadcast = target_req == 0x7DF
            if broadcast:
                # Mirror real-bus behaviour: every OBD-compliant ECU
                # responds to the functional broadcast address 0x7DF.
                ecus = list(self._fleet.values())
            else:
                specific = self._fleet.get(target_req)
                if specific is None:
                    return Response(error="NO DATA")
                ecus = [specific]

            self._advance_live_data()

            lines: list[str] = []
            for ecu in ecus:
                resp = self._dispatch(payload, ecu)
                if resp is not None and resp.lines:
                    lines.extend(resp.lines)
            if not lines:
                return Response(error="NO DATA")
            return Response(lines=lines)

    def _dispatch(self, payload: bytes, ecu: "_SimECU") -> Optional[Response]:
        service = payload[0]
        if service == 0x01:
            return self._handle_mode01(payload, ecu)
        if service == 0x03:
            return self._handle_mode03(ecu)
        if service == 0x04:
            return self._handle_mode04(ecu)
        if service == 0x07:
            return self._handle_mode07(ecu)
        if service == 0x09:
            return self._handle_mode09(payload, ecu)
        if service == 0x22:
            return self._handle_uds_read(payload, ecu)
        if service == 0x2F:
            return self._handle_uds_ioctl(payload, ecu)
        if service == 0x31:
            return self._handle_uds_routine(payload, ecu)
        return self._nrc(ecu, service, 0x11)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------
    def _resp_line(self, ecu: _SimECU, body: bytes) -> Response:
        """Build a single-line response with the 11-bit header prepended,
        matching the format the real ELM327 emits when ATH1 is on."""
        line = f"{ecu.response_id:03X}" + body.hex().upper()
        return Response(lines=[line])

    def _nrc(self, ecu: _SimECU, sid: int, code: int) -> Response:
        body = bytes([0x7F, sid, code])
        return self._resp_line(ecu, body)

    def _handle_mode01(self, payload: bytes, ecu: _SimECU) -> Optional[Response]:
        """Mode 01 handler.  Only the PCM reports live powertrain data;
        other ECUs respond only to PID 0x00 (supported-PIDs query) with
        an empty bitmap so they still appear in the module scan.
        Unsupported PIDs return None - real ECUs stay silent instead
        of emitting noisy NRCs on the functional broadcast."""
        if len(payload) < 2:
            return None
        pid = payload[1]
        if pid == 0x00:
            if ecu.name == "PCM":
                mask = 0
                for supported in (0x04, 0x05, 0x0C, 0x0D, 0x0F, 0x11):
                    mask |= 1 << (32 - supported)
            else:
                mask = 0
            body = bytes([0x41, 0x00]) + mask.to_bytes(4, "big")
            return self._resp_line(ecu, body)
        if ecu.name != "PCM":
            return None
        if pid == 0x04:
            return self._resp_line(ecu, bytes([0x41, 0x04, 40]))
        if pid == 0x05:
            return self._resp_line(ecu, bytes([0x41, 0x05, self._coolant + 40]))
        if pid == 0x0C:
            raw = self._rpm * 4
            return self._resp_line(
                ecu, bytes([0x41, 0x0C, (raw >> 8) & 0xFF, raw & 0xFF])
            )
        if pid == 0x0D:
            return self._resp_line(ecu, bytes([0x41, 0x0D, self._speed]))
        if pid == 0x0F:
            return self._resp_line(ecu, bytes([0x41, 0x0F, 65]))
        if pid == 0x11:
            return self._resp_line(ecu, bytes([0x41, 0x11, 35]))
        return None

    def _handle_mode03(self, ecu: _SimECU) -> Optional[Response]:
        if ecu.name != "PCM":
            return None
        count = len(self._stored_dtcs)
        body = bytes([0x43, count])
        for code in self._stored_dtcs:
            body += bytes([(code >> 8) & 0xFF, code & 0xFF])
        return self._resp_line(ecu, body)

    def _handle_mode04(self, ecu: _SimECU) -> Optional[Response]:
        if ecu.name != "PCM":
            return None
        self._stored_dtcs = []
        return self._resp_line(ecu, bytes([0x44]))

    def _handle_mode07(self, ecu: _SimECU) -> Optional[Response]:
        if ecu.name != "PCM":
            return None
        return self._resp_line(ecu, bytes([0x47, 0x00]))

    def _handle_mode09(self, payload: bytes, ecu: _SimECU) -> Optional[Response]:
        if ecu.name != "PCM":
            return None
        if len(payload) < 2:
            return None
        pid = payload[1]
        if pid == 0x02:
            body = bytes([0x49, 0x02, 0x01]) + _SIM_VIN.encode("ascii")
            return self._resp_line(ecu, body)
        return None

    def _handle_uds_read(self, payload: bytes, ecu: _SimECU) -> Response:
        if len(payload) != 3:
            return self._nrc(ecu, 0x22, 0x13)
        did = (payload[1] << 8) | payload[2]
        data = ecu.did_read.get(did)
        if data is None:
            return self._nrc(ecu, 0x22, 0x31)
        body = bytes([0x62, payload[1], payload[2]]) + data
        return self._resp_line(ecu, body)

    def _handle_uds_ioctl(self, payload: bytes, ecu: _SimECU) -> Response:
        if len(payload) < 4:
            return self._nrc(ecu, 0x2F, 0x13)
        did = (payload[1] << 8) | payload[2]
        control = payload[3]
        allowed = ecu.did_control.get(did)
        if allowed is None:
            return self._nrc(ecu, 0x2F, 0x31)
        if control not in allowed:
            return self._nrc(ecu, 0x2F, 0x22)  # conditionsNotCorrect
        if control == 0x00:
            ecu.controlled_dids.discard(did)
        else:
            ecu.controlled_dids.add(did)
        # Positive: 6F DID_H DID_L control [status]
        body = bytes([0x6F, payload[1], payload[2], control])
        return self._resp_line(ecu, body)

    def _handle_uds_routine(self, payload: bytes, ecu: _SimECU) -> Response:
        if len(payload) < 4:
            return self._nrc(ecu, 0x31, 0x13)
        sub = payload[1]
        rid = (payload[2] << 8) | payload[3]
        allowed = ecu.rid_routines.get(rid)
        if allowed is None:
            return self._nrc(ecu, 0x31, 0x31)
        if sub not in allowed:
            return self._nrc(ecu, 0x31, 0x12)  # subFunctionNotSupported
        # Positive: 71 sub RID_H RID_L status=00
        body = bytes([0x71, sub, payload[2], payload[3], 0x00])
        return self._resp_line(ecu, body)

    # ------------------------------------------------------------------
    def _advance_live_data(self) -> None:
        now = time.monotonic()
        if now - self._last_tick < 0.1:
            return
        self._last_tick = now
        self._rpm = max(650, min(3200, self._rpm + random.randint(-40, 50)))
        self._speed = max(0, min(120, self._speed + random.randint(-2, 3)))
        self._coolant = max(60, min(105, self._coolant + random.choice([-1, 0, 0, 1])))
