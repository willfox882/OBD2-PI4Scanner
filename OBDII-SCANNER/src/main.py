"""
Entry point for the OBD2 Diagnostic & Control Platform.

Startup ordering (safety-critical):

  1. Parse args, initialise file-only logging (never stdout/stderr,
     which would corrupt the curses UI).
  2. Decide whether to instantiate the real OBDConnection or the
     MockOBDConnection (OBD2_SIMULATOR=1).
  3. Load vehicle profiles from config/vehicle_profiles.
  4. Construct the SafetyGate.  This is the ONE authority that
     decides whether UDS / actuator commands may be transmitted.
  5. Construct live-data engine, DTC manager, capability detector,
     module scanner, bidirectional controller.  The bidirectional
     controller receives the SafetyGate so it fails closed.
  6. Connect to the adapter.  If a VIN can be read immediately, feed
     it to the SafetyGate so the UI starts in the right state.
  7. Run the TerminalUI.  On exit, cleanly stop live-data polling,
     end any active session, and disconnect the adapter.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .analysis.offline_analysis import OfflineAnalyzer
from .core.capability_detection import CapabilityDetector
from .core.config import AppConfig
from .core.logging_utils import get_logger, setup_logging
from .core.safety_gate import SafetyGate
from .core.vehicle_matrix import VehicleCompatibilityMatrix
from .obd.bidirectional import BidirectionalController
from .obd.dtc import DTCManager
from .obd.live_data import LiveDataEngine
from .obd.modules import ModuleScanner
from .obd.pids import PIDRegistry
from .session.session_manager import SessionManager
from .ui.menu_system import MenuSystem
from .ui.terminal_ui import TerminalUI


def _build_connection(port: str, baudrate: int, timeout: float, logger):
    simulator = os.environ.get("OBD2_SIMULATOR", "").strip() == "1"
    if simulator:
        logger.warning("OBD2_SIMULATOR=1 -> using MockOBDConnection")
        from .obd.simulator import MockOBDConnection
        return MockOBDConnection()
    from .core.connection import OBDConnection
    if not port:
        logger.error("No serial port supplied; pass --port or run with OBD2_SIMULATOR=1")
        sys.exit(2)
    return OBDConnection(port=port, baudrate=baudrate, timeout=timeout)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OBD2 Diagnostic & Control Platform"
    )
    parser.add_argument("--config", type=str, help="Path to YAML config file")
    parser.add_argument("--port", type=str, default="/dev/ttyUSB0")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    setup_logging(debug=args.debug)
    logger = get_logger("main")
    logger.info("Starting OBD2 Diagnostic Platform")

    config = AppConfig()
    if args.config:
        config.load(args.config)
    if args.port:
        config.set("connection.port", args.port)
    if args.baudrate:
        config.set("connection.baudrate", args.baudrate)

    conn = _build_connection(
        port=config.get("connection.port", args.port),
        baudrate=config.get("connection.baudrate", args.baudrate),
        timeout=config.get("connection.timeout", 2.0),
        logger=logger,
    )

    matrix = VehicleCompatibilityMatrix()
    matrix.load_profiles("config/vehicle_profiles")

    gate = SafetyGate()
    logger.info("SafetyGate mode=%s", gate.mode.name)

    pid_registry = PIDRegistry()
    session_mgr = SessionManager(log_dir=config.get("logging.log_dir", "./logs"))
    analyzer = OfflineAnalyzer(session_dir=config.get("logging.log_dir", "./logs"))
    engine = LiveDataEngine(conn, pid_registry, session_mgr)
    dtc_mgr = DTCManager(conn)
    cap_detector = CapabilityDetector(conn, gate)
    module_scanner = ModuleScanner(conn, matrix, gate)
    bidir_ctrl = BidirectionalController(conn, matrix, gate)

    menu = MenuSystem()
    ui = TerminalUI(
        menu=menu,
        engine=engine,
        dtc_mgr=dtc_mgr,
        module_scanner=module_scanner,
        bidir_ctrl=bidir_ctrl,
        cap_detector=cap_detector,
        session_mgr=session_mgr,
        analyzer=analyzer,
        conn=conn,
        safety_gate=gate,
    )

    try:
        if not conn.connect():
            logger.error("Initial connect failed; continuing in disconnected state")
        else:
            # Try to read the VIN at startup so the gate shows a
            # meaningful state from the first frame.  Non-fatal.
            try:
                vin = cap_detector.detect_vin()
                if vin:
                    gate.record_vin(vin)
                    logger.info("Startup VIN: %s", vin)
            except Exception as exc:
                logger.error("Startup VIN read failed: %s", exc)

        # Start a session unconditionally so the logger always records.
        session_mgr.start_session({"startup": "auto"})
        ui.run()
    except KeyboardInterrupt:
        logger.info("Exiting on SIGINT")
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
    finally:
        try:
            engine.stop()
        except Exception:
            pass
        try:
            if session_mgr.running:
                session_mgr.end_session()
        except Exception:
            pass
        try:
            conn.disconnect()
        except Exception:
            pass
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
