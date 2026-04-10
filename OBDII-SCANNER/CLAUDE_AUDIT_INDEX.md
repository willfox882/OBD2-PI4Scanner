# CLAUDE CODE AUDIT GUIDE & COMPACT INDEX
> **Context for Claude:** This is a token-optimized index of the OBD2 Diagnostic & Control Platform. Use this to perform targeted audits without blindly reading the entire codebase. 

## 🎯 Primary Audit Objectives
1. **Hardware Safety & Bus Collisions:** Verify `BidirectionalController` and `LiveDataEngine` do not collide on the CAN bus. Ensure `abort_test` safely returns ECU control.
2. **UDS/OBD Formatting:** Verify payload construction and Negative Response Code (NRC) parsing in `bidirectional.py` and `capability_tests.py`.
3. **Thread Safety:** Check locks in `SessionManager` and `LiveDataEngine` vs `TerminalUI` reads.
4. **Curses UI Stability:** Ensure `nodelay()` toggles and resize exceptions don't hang the SSH session.

## 📂 COMPACT FILE INDEX

### Core & Entry
* `/src/main.py` — App entry point. Initializes managers, handles graceful shutdown. *Audit:* Check `finally` block for proper thread/serial termination.
* `/src/core/connection.py` — `OBDConnection` serial wrapper. *Audit:* Check timeout handling and blocking reads on `pyserial`.
* `/src/core/config.py` — `AppConfig` dictionary wrapper. *Risk:* Low.
* `/src/core/errors.py` — Custom exceptions (`SafetyInterlock`). *Risk:* Low.
* `/src/core/logging_utils.py` — Standard Python logger setup. *Risk:* Low.
* `/src/core/vehicle_matrix.py` — Parses YAML profiles into `BidirCommand` dataclasses. *Audit:* Check for missing key handling (e.g., `get_module_address` returning None).
* `/src/core/capability_detection.py` — Builds `CapabilityReport`. *Audit:* `detect_uds_support` is currently a hardcoded mock.

### OBD & Bidirectional Logic (HIGH PRIORITY)
* `/src/obd/bidirectional.py` — Executes UDS commands, enforces `SafetyInterlock`, parses NRCs. *Audit:* Verify NRC parsing logic (`response[0] == 0x7F`) and `abort_test` fallback (`0x10 0x01`).
* `/src/obd/capability_tests.py` — Safely probes ECU (`0x22` or `0x2F 0x00`) without activating actuators. *Audit:* Verify `0x2F 0x00` logic is universally safe for Ford/GM.
* `/src/obd/live_data.py` — Async PID polling thread. *Audit:* Thread safety when `TerminalUI` calls `get_latest()`.
* `/src/obd/dtc.py` — Reads/clears DTCs. *Audit:* Verify UDS Mode 3/4 or 0x19/0x14 formatting.
* `/src/obd/modules.py` — Pings addresses to find active ECUs. *Risk:* Network timeout blocking.
* `/src/obd/pids.py` — Standard OBD2 PID definitions and decoders. *Risk:* Byte array out-of-bounds on malformed ECU responses.

### UI & Session Management
* `/src/ui/terminal_ui.py` — Main curses render loop, popups, and input handling. *Audit:* `_execute_bidir_test` pauses `LiveDataEngine`—verify this prevents bus collisions. Check `nodelay(0)` blocking in `_show_confirmation`.
* `/src/ui/menu_system.py` — State machine for UI navigation. *Risk:* Low.
* `/src/session/session_manager.py` — Manual CSV data logger (runs in background thread). *Audit:* Verify `threading.Lock()` usage around file I/O to prevent UI micro-stutters.
* `/src/analysis/offline_analysis.py` — Pandas-based CSV analyzer. *Risk:* High memory usage on large files (Raspberry Pi constraint).

### Configuration & Scripts
* `/config/vehicle_profiles/ford.yaml` & `gm.yaml` — Actuator definitions (Service, Subfunction, Data, Safety Level). *Audit:* Schema consistency.
* `/scripts/setup_hotspot.sh` & `setup_alias.sh` — Bash setup scripts for headless iPad access. *Risk:* OS-specific dependencies (nmcli).
* `/test_ui_windows.bat` — Windows sandbox launcher. *Risk:* Low.
