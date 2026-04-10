"""
SafetyGate: the ONLY place that decides whether a UDS/actuator command
is allowed to reach the vehicle.

Operating modes (see README + project safety spec):

  UNIVERSAL           Default for every unknown vehicle.  Permits
                      Mode 01-09 reads + Mode 03/04 DTC read/clear.
                      All UDS / actuator / routine services are hard
                      disabled.

  GMC_SIERRA_2015     Unlocked only when:
                        1. VIN has been read from the vehicle,
                        2. VIN matches a 2015 GMC Sierra 1500 4.3L LV3,
                        3. the operator explicitly enables advanced
                           mode in the UI.
                      In this mode carefully scoped UDS 0x22/0x2F/0x31
                      requests are permitted, BUT ONLY against
                      DIDs/RIDs that are explicitly defined *and*
                      flagged verified in the vehicle profile YAML.

  SIMULATOR           Enabled by the environment variable
                      OBD2_SIMULATOR=1.  Permits the full UDS sandbox
                      against the mock ECU and NEVER against real
                      hardware.  main.py is responsible for actually
                      swapping in MockOBDConnection when this mode is
                      active - SafetyGate only records the fact.

The gate does NOT encode what the commands *do* - it only decides
whether a command is allowed to be transmitted at all.  Framing and
per-command `enabled`/`verified` flags live in the vehicle profile.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class GateMode(Enum):
    UNIVERSAL = "universal"
    GMC_SIERRA_2015 = "gmc_sierra_2015"
    SIMULATOR = "simulator"


# 2015 GMC Sierra 1500 4.3L LV3 V6 VIN signature.
#
#   Position  1-3  World Manufacturer Identifier (WMI): 3GT = GMC truck
#                                                      (2WD/4WD variants
#                                                      start 1GT / 3GT)
#   Position  8    Engine code: H = 4.3L LV3 V6
#   Position 10    Model year:  F = 2015
#
# We match positions 1,8,10 and treat the rest as free.  We deliberately
# *also* allow the "1GT" WMI because GM uses 1GT for US-built and 3GT
# for Mexico-built light-duty trucks in this generation.  Anything else
# is rejected.
_ALLOWED_WMIS = ("1GT", "3GT")
_REQUIRED_ENGINE_CODE = "H"   # VIN position 8
_REQUIRED_MODEL_YEAR = "F"    # VIN position 10  -> 2015


@dataclass
class VinDecision:
    vin: str
    matches_gmc_sierra_2015_lv3: bool
    reason: str


def decode_vin(vin: Optional[str]) -> VinDecision:
    """Very narrow VIN validator.  Returns why a VIN was accepted or
    rejected so the UI can show the operator a clear message.

    This function is deliberately paranoid: any unexpected length,
    character, or positional mismatch -> rejection.
    """
    if not vin:
        return VinDecision("", False, "no VIN available")
    vin = vin.strip().upper()
    if len(vin) != 17:
        return VinDecision(vin, False, f"VIN length {len(vin)} != 17")
    if any(c in "IOQ" for c in vin):
        # Illegal VIN characters per ISO 3779.
        return VinDecision(vin, False, "VIN contains illegal characters")
    wmi = vin[0:3]
    if wmi not in _ALLOWED_WMIS:
        return VinDecision(vin, False, f"WMI {wmi} is not a GMC light-duty truck")
    engine = vin[7]  # position 8 (0-indexed 7)
    if engine != _REQUIRED_ENGINE_CODE:
        return VinDecision(
            vin, False, f"engine code {engine} != H (4.3L LV3)"
        )
    year = vin[9]  # position 10 (0-indexed 9)
    if year != _REQUIRED_MODEL_YEAR:
        return VinDecision(vin, False, f"model year code {year} != F (2015)")
    return VinDecision(vin, True, "match: 2015 GMC Sierra 1500 4.3L LV3")


class SafetyGate:
    """Central authority for permitting UDS / actuator control."""

    def __init__(self) -> None:
        self._logger = logging.getLogger("SafetyGate")
        self._mode: GateMode = GateMode.UNIVERSAL
        self._vin: Optional[str] = None
        self._vin_decision: Optional[VinDecision] = None
        self._advanced_mode_enabled: bool = False
        self._simulator = os.environ.get("OBD2_SIMULATOR", "").strip() == "1"
        if self._simulator:
            self._mode = GateMode.SIMULATOR
            self._logger.warning(
                "SIMULATOR MODE: real hardware writes are inert"
            )

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @property
    def mode(self) -> GateMode:
        return self._mode

    @property
    def simulator(self) -> bool:
        return self._simulator

    @property
    def vin(self) -> Optional[str]:
        return self._vin

    @property
    def vin_decision(self) -> Optional[VinDecision]:
        return self._vin_decision

    @property
    def advanced_mode_enabled(self) -> bool:
        return self._advanced_mode_enabled

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def record_vin(self, vin: Optional[str]) -> VinDecision:
        """Record the VIN read from the vehicle (or simulator).

        In simulator mode, we still run the VIN check so the mock ECU
        exercises the same code paths as a real truck.
        """
        decision = decode_vin(vin)
        self._vin = decision.vin or None
        self._vin_decision = decision
        self._logger.info(
            "VIN recorded: %s -> match=%s (%s)",
            decision.vin or "<none>",
            decision.matches_gmc_sierra_2015_lv3,
            decision.reason,
        )
        # Downgrade advanced mode if a previously-matched VIN was wrong.
        if not decision.matches_gmc_sierra_2015_lv3:
            if self._mode == GateMode.GMC_SIERRA_2015:
                self._mode = GateMode.UNIVERSAL
                self._advanced_mode_enabled = False
                self._logger.warning(
                    "VIN changed / mismatch - reverting to UNIVERSAL mode"
                )
        return decision

    def enable_advanced_mode(self) -> bool:
        """Operator acknowledges the danger and unlocks scoped UDS.

        Returns True on success.  Fails closed if the VIN does not
        match the approved 2015 GMC Sierra profile and we are not in
        simulator mode.
        """
        if self._simulator:
            self._mode = GateMode.SIMULATOR
            self._advanced_mode_enabled = True
            self._logger.info("Advanced mode enabled (simulator)")
            return True
        if (
            self._vin_decision is not None
            and self._vin_decision.matches_gmc_sierra_2015_lv3
        ):
            self._mode = GateMode.GMC_SIERRA_2015
            self._advanced_mode_enabled = True
            self._logger.warning(
                "Advanced mode enabled for VIN %s", self._vin
            )
            return True
        self._logger.warning(
            "enable_advanced_mode refused: VIN does not match "
            "2015 GMC Sierra 1500 4.3L LV3 profile"
        )
        return False

    def disable_advanced_mode(self) -> None:
        self._advanced_mode_enabled = False
        if self._mode == GateMode.GMC_SIERRA_2015:
            self._mode = GateMode.UNIVERSAL
        self._logger.info("Advanced mode disabled")

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------
    def allows_obd_read(self) -> bool:
        """Mode 01, 02, 05, 06, 08, 09 reads.  Always permitted."""
        return True

    def allows_dtc_read(self) -> bool:
        """Mode 03 / 07 / 0A DTC reads.  Always permitted."""
        return True

    def allows_dtc_clear(self) -> bool:
        """Mode 04 DTC clear.  Always permitted (standard OBD-II)."""
        return True

    def allows_uds_read(self) -> bool:
        """UDS 0x22 ReadDataByIdentifier.

        Read-only but non-standard; restricted to advanced mode.
        """
        return self._advanced_mode_enabled and self._mode in (
            GateMode.GMC_SIERRA_2015,
            GateMode.SIMULATOR,
        )

    def allows_uds_actuator(self) -> bool:
        """UDS 0x2F InputOutputControlByIdentifier.

        Bidirectional actuator control.  Hard-disabled unless both
        VIN matches (or simulator) AND operator enabled advanced mode.
        """
        return self._advanced_mode_enabled and self._mode in (
            GateMode.GMC_SIERRA_2015,
            GateMode.SIMULATOR,
        )

    def allows_uds_routine(self) -> bool:
        """UDS 0x31 RoutineControl.  Same constraints as 0x2F."""
        return self.allows_uds_actuator()

    def reason_blocked(self) -> str:
        """Human-readable explanation of why advanced commands are
        currently disabled.  Used by the UI."""
        if self._simulator:
            if not self._advanced_mode_enabled:
                return "Simulator ready. Enable advanced mode to sandbox UDS."
            return "Simulator unlocked."
        if self._vin_decision is None:
            return (
                "VIN has not been read yet. Advanced commands are "
                "disabled until the vehicle identifies itself."
            )
        if not self._vin_decision.matches_gmc_sierra_2015_lv3:
            return (
                "Connected vehicle is not the approved 2015 GMC Sierra "
                f"1500 4.3L LV3 ({self._vin_decision.reason}). "
                "Advanced commands are permanently disabled for this "
                "vehicle."
            )
        if not self._advanced_mode_enabled:
            return (
                "VIN approved. Enable advanced mode from the menu to "
                "unlock scoped bidirectional controls."
            )
        return "Advanced mode active."
