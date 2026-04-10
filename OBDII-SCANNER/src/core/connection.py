"""
OBDConnection: thread-safe ELM327 / STN1110 serial wrapper.

Design notes (safety critical):
  * All writes go through a single re-entrant lock so no two threads can
    interleave a command/response sequence on the serial port.
  * The class tracks the currently-set CAN header (ATSH) and response
    address filter (ATCRA) so callers never have to guess whether the
    adapter is in broadcast mode (0x7DF functional) or targeted mode
    (e.g. 0x7E0 ECM). set_header() is a no-op when the header is
    already correct, which avoids round-tripping the adapter.
  * query() returns a structured Response object (lines + raw) so
    downstream parsers can work line-by-line instead of fishing
    substrings out of concatenated hex dumps.
  * Reconnect is explicit and bounded; we do NOT try to silently
    reconnect in the middle of a bidirectional test because that
    would leave the ECU in an unknown state.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Union

try:
    import serial  # type: ignore
except ImportError:  # pragma: no cover - serial only required on real HW
    serial = None  # type: ignore


class ConnectionState(Enum):
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2
    ERROR = 3


# ELM327 / ISO-15765 functional broadcast address (OBD-II standard).
FUNCTIONAL_BROADCAST_11BIT = "7DF"


@dataclass
class Response:
    """Structured response from the ELM327.

    ``lines`` is a list of cleaned upper-case hex strings with whitespace
    removed, one per ELM327 reply line.  ``raw`` is the original bytes for
    debugging.  ``error`` is set when the adapter returned a textual error
    (``NO DATA``, ``CAN ERROR``, ``BUFFER FULL``, etc.).
    """

    raw: bytes = b""
    lines: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.lines)

    def first_hex(self) -> str:
        return self.lines[0] if self.lines else ""

    def joined_hex(self) -> str:
        return "".join(self.lines)


class OBDConnection:
    """Thread-safe wrapper around the ELM327 serial port.

    IMPORTANT: instance of this class is shared between the UI thread,
    the live-data polling thread, and any bidirectional/diagnostic
    threads.  All three may race each other.  A single RLock serialises
    every on-wire transaction end-to-end.
    """

    # Textual error tokens the ELM327 can emit (any line, any case).
    _ERROR_TOKENS = (
        "NO DATA",
        "NODATA",
        "CAN ERROR",
        "BUS ERROR",
        "BUS BUSY",
        "BUS INIT",
        "FB ERROR",
        "DATA ERROR",
        "BUFFER FULL",
        "STOPPED",
        "UNABLE TO CONNECT",
        "?",
    )

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 2.0):
        self.port_name = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.state = ConnectionState.DISCONNECTED
        self.voltage: float = 0.0
        self.protocol_name: str = "Auto"

        self._serial = None
        self._lock = threading.RLock()
        self._current_header: Optional[str] = None
        self._current_filter: Optional[str] = None
        self._logger = logging.getLogger("OBDConnection")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def connect(self) -> bool:
        with self._lock:
            if self.state == ConnectionState.CONNECTED:
                return True
            self.state = ConnectionState.CONNECTING
            self._logger.info(
                "Connecting to %s @ %d baud", self.port_name, self.baudrate
            )

            if serial is None:
                self._logger.error("pyserial not installed - cannot open port")
                self.state = ConnectionState.ERROR
                return False

            try:
                self._serial = serial.Serial(
                    self.port_name,
                    self.baudrate,
                    timeout=self.timeout,
                    write_timeout=self.timeout,
                )
            except Exception as exc:
                self._logger.error("serial.Serial failed: %s", exc)
                self.state = ConnectionState.ERROR
                return False

            # Drain any boot garbage.
            try:
                self._serial.reset_input_buffer()
                self._serial.reset_output_buffer()
            except Exception:
                pass

            try:
                # ELM327 init sequence.  We verify each command returns OK
                # because a silent failure here = undefined adapter state.
                init_cmds: list[tuple[str, str]] = [
                    ("ATZ", "ELM"),       # reset; response contains 'ELM327'
                    ("ATE0", "OK"),       # echo off
                    ("ATL0", "OK"),       # linefeeds off
                    ("ATS0", "OK"),       # spaces off
                    ("ATH1", "OK"),       # headers on (we parse them)
                    ("ATCAF1", "OK"),     # CAN auto-formatting (PCI bytes handled by chip)
                    ("ATSP0", "OK"),      # protocol: auto
                    ("ATAT1", "OK"),      # adaptive timing normal
                ]
                for cmd, expect in init_cmds:
                    resp = self._raw_txn(cmd + "\r")
                    if expect not in resp.upper():
                        # ATZ emits a banner like "ELM327 v1.5" - substring
                        # check handles that.  Anything else must match OK.
                        self._logger.error(
                            "Init command %s failed (resp=%r)", cmd, resp
                        )
                        self._close_serial()
                        self.state = ConnectionState.ERROR
                        return False
                    if cmd == "ATZ":
                        time.sleep(1.0)  # let chip settle after reset

                # Read battery voltage (informational, non-fatal).
                v_resp = self._raw_txn("ATRV\r")
                try:
                    self.voltage = float(v_resp.replace("V", "").strip())
                except (ValueError, AttributeError):
                    self.voltage = 0.0

                # Query the adapter's chosen protocol (informational).
                p_resp = self._raw_txn("ATDPN\r")
                self.protocol_name = p_resp.strip() or "Auto"

                self._current_header = None
                self._current_filter = None
                self.state = ConnectionState.CONNECTED
                self._logger.info(
                    "ELM327 initialised; voltage=%.1fV protocol=%s",
                    self.voltage,
                    self.protocol_name,
                )
                return True
            except Exception as exc:
                self._logger.exception("Unexpected init failure: %s", exc)
                self._close_serial()
                self.state = ConnectionState.ERROR
                return False

    def disconnect(self) -> None:
        with self._lock:
            # Best-effort: drop any targeted header / filter so the
            # next user of this physical adapter starts in a known
            # broadcast state.  Failures here are ignored.
            if self.state == ConnectionState.CONNECTED:
                try:
                    self._raw_txn("ATAR\r")  # auto receive
                    self._raw_txn("ATSH 7DF\r")  # broadcast header
                except Exception:
                    pass
            self._close_serial()
            self.state = ConnectionState.DISCONNECTED
            self._current_header = None
            self._current_filter = None
            self._logger.info("Disconnected")

    def _close_serial(self) -> None:
        try:
            if self._serial is not None and getattr(self._serial, "is_open", False):
                self._serial.close()
        except Exception:
            pass
        self._serial = None

    def reconnect(self, attempts: int = 3, delay: float = 2.0) -> bool:
        """Explicit bounded reconnect.  Callers decide when to invoke."""
        with self._lock:
            self._close_serial()
            self.state = ConnectionState.DISCONNECTED
            for i in range(attempts):
                self._logger.info("Reconnect attempt %d/%d", i + 1, attempts)
                if self.connect():
                    return True
                time.sleep(delay)
            return False

    def is_alive(self) -> bool:
        return self.state == ConnectionState.CONNECTED

    # ------------------------------------------------------------------
    # Header / filter management
    # ------------------------------------------------------------------
    def set_header(self, header: str) -> bool:
        """Set the CAN transmit header (ATSH).

        ``header`` must be a 3-character (11-bit CAN) or 8-character
        (29-bit) hex string with no ``0x`` prefix, e.g. ``"7E0"``.
        Returns True if the header is now active (idempotent).
        """
        header = header.upper().strip()
        if len(header) not in (3, 8) or any(c not in "0123456789ABCDEF" for c in header):
            self._logger.error("Refusing malformed header: %r", header)
            return False
        with self._lock:
            if self._current_header == header:
                return True
            if not self.is_alive():
                return False
            resp = self._raw_txn(f"ATSH {header}\r")
            if "OK" not in resp.upper():
                self._logger.error("ATSH %s failed: %r", header, resp)
                return False
            self._current_header = header
            return True

    def set_response_filter(self, addr: Optional[str]) -> bool:
        """Set ATCRA (CAN Receive Address) or clear it with None.

        Keeping the response filter tight prevents cross-chatter from
        other modules landing in our parser.
        """
        with self._lock:
            if not self.is_alive():
                return False
            if addr is None:
                if self._current_filter is None:
                    return True
                resp = self._raw_txn("ATAR\r")  # auto receive
                if "OK" not in resp.upper():
                    return False
                self._current_filter = None
                return True
            addr = addr.upper().strip()
            if self._current_filter == addr:
                return True
            resp = self._raw_txn(f"ATCRA {addr}\r")
            if "OK" not in resp.upper():
                self._logger.error("ATCRA %s failed: %r", addr, resp)
                return False
            self._current_filter = addr
            return True

    def reset_header(self) -> bool:
        """Return the adapter to the OBD-II functional broadcast header."""
        with self._lock:
            self.set_response_filter(None)
            return self.set_header(FUNCTIONAL_BROADCAST_11BIT)

    @property
    def current_header(self) -> Optional[str]:
        return self._current_header

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def query(self, command: Union[str, bytes]) -> Response:
        """Send one command and return a structured Response.

        ``command`` may be a string (AT command or already-formatted hex
        like ``"0100"``) or a bytes object whose hex representation will
        be sent.  The call is thread-safe and atomic.
        """
        with self._lock:
            if not self.is_alive():
                return Response(error="DISCONNECTED")

            if isinstance(command, bytes):
                cmd_str = command.hex().upper()
            else:
                cmd_str = command.strip()

            try:
                raw = self._raw_txn(cmd_str + "\r", return_bytes=True)
            except Exception as exc:
                self._logger.error("Query %r failed: %s", cmd_str, exc)
                self.state = ConnectionState.ERROR
                return Response(error=f"IO:{exc}")

            return self._parse_response(raw)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _raw_txn(self, command: str, return_bytes: bool = False):
        """Write bytes and read until ``>`` prompt or timeout.

        Must be called with self._lock held.  Returns a decoded string
        by default, or the raw bytes when return_bytes=True.
        """
        if self._serial is None:
            return b"" if return_bytes else ""

        try:
            self._serial.reset_input_buffer()
        except Exception:
            pass

        self._serial.write(command.encode("ascii"))
        try:
            self._serial.flush()
        except Exception:
            pass

        buf = bytearray()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                n = self._serial.in_waiting
            except Exception:
                n = 0
            if n:
                chunk = self._serial.read(n)
                if not chunk:
                    continue
                buf.extend(chunk)
                if b">" in chunk:
                    break
            else:
                time.sleep(0.005)

        raw = bytes(buf)
        # Strip the trailing prompt.
        if b">" in raw:
            raw = raw[: raw.rfind(b">")]
        if return_bytes:
            return raw
        try:
            return raw.decode("ascii", errors="replace")
        except Exception:
            return ""

    @classmethod
    def _parse_response(cls, raw: bytes) -> Response:
        """Split raw bytes into cleaned hex lines + error classification."""
        if not raw:
            return Response(raw=raw, error="EMPTY")

        text = raw.decode("ascii", errors="replace")
        lines: list[str] = []
        error: Optional[str] = None

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            upper = stripped.upper()
            # Classify textual errors from the adapter.
            for tok in cls._ERROR_TOKENS:
                if tok in upper:
                    error = tok
                    break
            if error is not None:
                continue
            # Strip whitespace, colons, tabs; keep only hex nibbles.
            hex_only = "".join(c for c in upper if c in "0123456789ABCDEF")
            if hex_only:
                lines.append(hex_only)

        if not lines and error is None:
            error = "EMPTY"
        return Response(raw=raw, lines=lines, error=error)
