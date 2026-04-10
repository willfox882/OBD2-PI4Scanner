"""
Vehicle profile loader.

The profile schema is deliberately narrow - there is exactly ONE
bidirectional command type (``BidirCommand``) which carries either a
ReadDataByIdentifier (0x22), an InputOutputControlByIdentifier (0x2F),
or a RoutineControl (0x31) payload.  No other UDS service is modelled.

Each command MUST declare:
  * ``module``         Symbolic module name (e.g. ``ECM``).  Must be
                       present in the profile's ``modules`` mapping.
  * ``service``        0x22 | 0x2F | 0x31.  Anything else is rejected
                       at load time.
  * ``did`` / ``rid``  16-bit identifier.
  * ``enabled``        Must be explicitly ``true`` for the command to
                       be runnable.  The default (if omitted) is FALSE.
                       Use this to ship commands whose DIDs are not yet
                       verified against an FSM.
  * ``verified``       Independent flag recording whether the command
                       has been verified against an OEM service manual.
                       The BidirectionalController refuses any command
                       whose ``verified`` flag is false, even if
                       ``enabled`` is true.
  * ``safety_level``   SAFE | CAUTION | DANGER.

For 0x2F commands:
  * ``control``        IOC parameter (0x00..0x03 per ISO 14229).
  * ``states``         Optional list of controlState bytes.

For 0x31 commands:
  * ``subfunction``    routineControlType (0x01 start, 0x02 stop,
                       0x03 requestResults).
  * ``data``           Optional routineControlOptionRecord bytes.

For 0x22 commands:
  * ``data``           Ignored.  The DID alone is the request.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import yaml


class SafetyLevel(Enum):
    SAFE = 1
    CAUTION = 2
    DANGER = 3


# Allowed services - enforced at load time.
_ALLOWED_SERVICES = (0x22, 0x2F, 0x31)


@dataclass
class ModuleSpec:
    """One ECU on the bus."""

    name: str
    request_id: int   # ATSH target, 11-bit or 29-bit
    response_id: int  # ATCRA filter
    description: str = ""


@dataclass
class BidirCommand:
    name: str
    description: str
    module: str
    service: int

    # 0x22 / 0x2F use did.  0x31 uses rid.
    did: Optional[int] = None
    rid: Optional[int] = None

    # 0x2F-only.
    control: Optional[int] = None  # IOControlParameter
    states: list[int] = field(default_factory=list)

    # 0x31-only.
    subfunction: Optional[int] = None  # routineControlType

    # 0x31 optional data record.
    data: list[int] = field(default_factory=list)

    safety_level: SafetyLevel = SafetyLevel.DANGER
    enabled: bool = False
    verified: bool = False
    notes: str = ""

    # ------------------------------------------------------------------
    # Frame builder - single authoritative formatter.  Used by the
    # bidirectional controller AND the simulator.
    # ------------------------------------------------------------------
    def build_payload(self) -> bytes:
        """Build the on-wire UDS request bytes (without CAN PCI).

        Raises ValueError if the command is malformed.
        """
        if self.service == 0x22:
            if self.did is None:
                raise ValueError(f"{self.name}: 0x22 command requires a DID")
            return bytes([0x22, (self.did >> 8) & 0xFF, self.did & 0xFF])

        if self.service == 0x2F:
            if self.did is None:
                raise ValueError(f"{self.name}: 0x2F command requires a DID")
            if self.control is None or self.control not in (0x00, 0x01, 0x02, 0x03):
                raise ValueError(
                    f"{self.name}: 0x2F command requires control 0x00..0x03"
                )
            frame = bytes(
                [
                    0x2F,
                    (self.did >> 8) & 0xFF,
                    self.did & 0xFF,
                    self.control,
                ]
            )
            frame += bytes(b & 0xFF for b in self.states)
            return frame

        if self.service == 0x31:
            if self.rid is None:
                raise ValueError(f"{self.name}: 0x31 command requires an RID")
            if self.subfunction not in (0x01, 0x02, 0x03):
                raise ValueError(
                    f"{self.name}: 0x31 subfunction must be 0x01|0x02|0x03"
                )
            frame = bytes(
                [
                    0x31,
                    self.subfunction,
                    (self.rid >> 8) & 0xFF,
                    self.rid & 0xFF,
                ]
            )
            frame += bytes(b & 0xFF for b in self.data)
            return frame

        raise ValueError(f"{self.name}: unsupported service {hex(self.service)}")

    def abort_payload(self) -> Optional[bytes]:
        """Return the bytes that SAFELY cancel this command.

        * 0x2F -> ReturnControlToECU (control=0x00) for the same DID.
        * 0x31 -> stopRoutine (subfunction=0x02) for the same RID.
        * 0x22 -> None (read-only, nothing to abort).
        """
        if self.service == 0x2F and self.did is not None:
            return bytes(
                [
                    0x2F,
                    (self.did >> 8) & 0xFF,
                    self.did & 0xFF,
                    0x00,  # returnControlToECU
                ]
            )
        if self.service == 0x31 and self.rid is not None:
            return bytes(
                [
                    0x31,
                    0x02,  # stopRoutine
                    (self.rid >> 8) & 0xFF,
                    self.rid & 0xFF,
                ]
            )
        return None


@dataclass
class VehicleProfile:
    oem: str
    name: str
    vin_prefix: str  # informational match hint (actual check in SafetyGate)
    modules: dict[str, ModuleSpec]
    commands: list[BidirCommand]

    def get_module(self, name: str) -> Optional[ModuleSpec]:
        return self.modules.get(name)


class VehicleCompatibilityMatrix:
    def __init__(self) -> None:
        self.profiles: dict[str, VehicleProfile] = {}
        self._logger = logging.getLogger("VehicleMatrix")

    # ------------------------------------------------------------------
    def load_profiles(self, directory: str) -> None:
        if not os.path.isdir(directory):
            self._logger.warning("Profile dir %s missing", directory)
            return
        for fname in sorted(os.listdir(directory)):
            if not fname.endswith((".yaml", ".yml")):
                continue
            path = os.path.join(directory, fname)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
            except Exception as exc:
                self._logger.error("YAML load failed for %s: %s", fname, exc)
                continue
            try:
                profile = self._build_profile(data)
            except Exception as exc:
                self._logger.error("Profile parse failed for %s: %s", fname, exc)
                continue
            self.profiles[profile.oem] = profile
            self._logger.info(
                "Loaded profile %s (%d commands, %d enabled+verified)",
                profile.oem,
                len(profile.commands),
                sum(1 for c in profile.commands if c.enabled and c.verified),
            )

    # ------------------------------------------------------------------
    def _build_profile(self, data: dict) -> VehicleProfile:
        oem = str(data["oem"])
        name = str(data.get("name", oem))
        vin_prefix = str(data.get("vin_prefix", ""))

        modules: dict[str, ModuleSpec] = {}
        for mod_name, spec in (data.get("modules") or {}).items():
            if isinstance(spec, int):
                # Back-compat with shorthand: ECM: 0x7E0 -> response inferred.
                req = int(spec)
                resp = req + 0x08 if req < 0x7F0 else req
                modules[mod_name] = ModuleSpec(mod_name, req, resp)
            else:
                modules[mod_name] = ModuleSpec(
                    name=mod_name,
                    request_id=int(spec["request"]),
                    response_id=int(spec["response"]),
                    description=str(spec.get("description", "")),
                )

        commands: list[BidirCommand] = []
        for raw in data.get("bidirectional", []) or []:
            try:
                service = int(raw["service"])
                if service not in _ALLOWED_SERVICES:
                    raise ValueError(
                        f"service {hex(service)} not in allow-list"
                    )
                module = str(raw["module"])
                if module not in modules:
                    raise ValueError(f"unknown module {module}")

                cmd = BidirCommand(
                    name=str(raw["name"]),
                    description=str(raw.get("description", "")),
                    module=module,
                    service=service,
                    did=_opt_int(raw.get("did")),
                    rid=_opt_int(raw.get("rid")),
                    control=_opt_int(raw.get("control")),
                    states=[int(b) for b in raw.get("states", []) or []],
                    subfunction=_opt_int(raw.get("subfunction")),
                    data=[int(b) for b in raw.get("data", []) or []],
                    safety_level=SafetyLevel[
                        str(raw.get("safety_level", "DANGER")).upper()
                    ],
                    enabled=bool(raw.get("enabled", False)),
                    verified=bool(raw.get("verified", False)),
                    notes=str(raw.get("notes", "")),
                )
                # Construct the payload once to fail fast on bad data.
                cmd.build_payload()
                commands.append(cmd)
            except Exception as exc:
                self._logger.error(
                    "Skipping malformed command %r: %s",
                    raw.get("name"),
                    exc,
                )

        return VehicleProfile(
            oem=oem,
            name=name,
            vin_prefix=vin_prefix,
            modules=modules,
            commands=commands,
        )

    # ------------------------------------------------------------------
    def get_profile(self, oem: str) -> Optional[VehicleProfile]:
        return self.profiles.get(oem)

    def get_bidir_commands(self, oem: str) -> list[BidirCommand]:
        profile = self.profiles.get(oem)
        return list(profile.commands) if profile else []


def _opt_int(value) -> Optional[int]:
    if value is None:
        return None
    return int(value)
