"""
PIDRegistry - hardened decoders for a curated set of Mode 01 PIDs.

Every decoder is defensive about byte length and returns ``None``
instead of raising.  The registry extracts the data bytes from a
positive response ``41 PID [data...]`` by finding the tag inside any
Response line, not by position.  Multiple ECU echoes are tolerated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional, Union


@dataclass
class DecodedValue:
    pid_id: str
    name: str
    raw: str
    value: Union[float, int, str]
    unit: str
    is_suspect: bool
    timestamp: datetime


@dataclass
class PIDDefinition:
    pid: str
    name: str
    unit: str
    min_bytes: int
    decoder: Callable[[list[int]], Optional[float]]


def _rpm(d: list[int]) -> Optional[float]:
    if len(d) < 2:
        return None
    return ((d[0] * 256) + d[1]) / 4.0


def _speed(d: list[int]) -> Optional[float]:
    if len(d) < 1:
        return None
    return float(d[0])


def _temp(d: list[int]) -> Optional[float]:
    if len(d) < 1:
        return None
    return float(d[0] - 40)


def _percent_255(d: list[int]) -> Optional[float]:
    if len(d) < 1:
        return None
    return (d[0] * 100.0) / 255.0


def _maf(d: list[int]) -> Optional[float]:
    if len(d) < 2:
        return None
    return ((d[0] * 256) + d[1]) / 100.0


class PIDRegistry:
    def __init__(self) -> None:
        self.pids: dict[str, PIDDefinition] = {
            "04": PIDDefinition("04", "Engine Load", "%", 1, _percent_255),
            "05": PIDDefinition("05", "Coolant Temp", "C", 1, _temp),
            "0B": PIDDefinition("0B", "Intake MAP", "kPa", 1, lambda d: float(d[0]) if d else None),
            "0C": PIDDefinition("0C", "Engine RPM", "rpm", 2, _rpm),
            "0D": PIDDefinition("0D", "Vehicle Speed", "km/h", 1, _speed),
            "0F": PIDDefinition("0F", "Intake Air Temp", "C", 1, _temp),
            "10": PIDDefinition("10", "MAF Airflow", "g/s", 2, _maf),
            "11": PIDDefinition("11", "Throttle Position", "%", 1, _percent_255),
            "2F": PIDDefinition("2F", "Fuel Level", "%", 1, _percent_255),
            "42": PIDDefinition("42", "Control Module Voltage", "V",
                                 2, lambda d: (((d[0] << 8) | d[1]) / 1000.0) if len(d) >= 2 else None),
            "46": PIDDefinition("46", "Ambient Air Temp", "C", 1, _temp),
            "5C": PIDDefinition("5C", "Oil Temp", "C", 1, _temp),
        }

    def decode(self, pid: str, raw_hex: str) -> Optional[DecodedValue]:
        if not raw_hex or pid not in self.pids:
            return None
        defn = self.pids[pid]
        tag = f"41{pid.upper()}"
        idx = raw_hex.upper().find(tag)
        if idx == -1:
            return None
        data_hex = raw_hex[idx + len(tag) :]
        # Extract pairs of hex chars, cap at the minimum needed +4 so
        # trailing CAN padding doesn't corrupt us.
        bytes_needed = defn.min_bytes
        if len(data_hex) < bytes_needed * 2:
            return None
        try:
            data = [
                int(data_hex[i : i + 2], 16)
                for i in range(0, bytes_needed * 2, 2)
            ]
        except ValueError:
            return None
        try:
            value = defn.decoder(data)
        except Exception:
            return None
        if value is None:
            return None
        if isinstance(value, float):
            value = round(value, 2)
        return DecodedValue(
            pid_id=pid,
            name=defn.name,
            raw=raw_hex,
            value=value,
            unit=defn.unit,
            is_suspect=False,
            timestamp=datetime.now(),
        )
