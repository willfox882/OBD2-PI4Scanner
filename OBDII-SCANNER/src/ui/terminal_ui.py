"""
TerminalUI - curses front-end for iPad SSH / laptop terminal use.

Design notes:
  * Uses curses.wrapper() so TTY state is always restored on crash.
  * Handles KEY_RESIZE explicitly so a Termius / SSH client resize
    never leaves the screen in a garbage state.
  * Never writes to the bottom-right cell (curses raises an error
    on that coordinate on many terminals).
  * All popup windows restore nodelay(1) in a finally block.
  * The BIDIRECTIONAL menu is ONLY reachable after the operator has
    unlocked advanced mode via BIDIR_GATE.  Every bidir execution
    pauses the LiveDataEngine first, runs the command, then resumes
    polling in a finally block.
  * On every request to run a CAUTION / DANGER command we force the
    user through a red-background confirmation popup.
"""

from __future__ import annotations

import curses
import logging
import time
from typing import Optional

from ..analysis.offline_analysis import OfflineAnalyzer
from ..core.capability_detection import CapabilityDetector
from ..core.connection import OBDConnection
from ..core.errors import SafetyInterlock
from ..core.safety_gate import GateMode, SafetyGate
from ..core.vehicle_matrix import BidirCommand, SafetyLevel
from ..obd.bidirectional import BidirectionalController, TestResult
from ..obd.dtc import DTCManager
from ..obd.live_data import LiveDataEngine
from ..obd.modules import ModuleScanner
from ..session.session_manager import SessionManager
from .menu_system import MenuState, MenuSystem


BIDIR_PROFILE_OEM = "gmc_2015_sierra_43lv3"


class TerminalUI:
    def __init__(
        self,
        menu: MenuSystem,
        engine: LiveDataEngine,
        dtc_mgr: DTCManager,
        module_scanner: ModuleScanner,
        bidir_ctrl: BidirectionalController,
        cap_detector: CapabilityDetector,
        session_mgr: SessionManager,
        analyzer: OfflineAnalyzer,
        conn: OBDConnection,
        safety_gate: SafetyGate,
    ) -> None:
        self.menu = menu
        self.engine = engine
        self.dtc_mgr = dtc_mgr
        self.module_scanner = module_scanner
        self.bidir_ctrl = bidir_ctrl
        self.cap_detector = cap_detector
        self.session_mgr = session_mgr
        self.analyzer = analyzer
        self.conn = conn
        self.gate = safety_gate

        self.stdscr = None
        self.selected_idx: int = 0
        self._logger = logging.getLogger("TerminalUI")

    # ------------------------------------------------------------------
    def run(self) -> None:
        try:
            self.engine.set_active_pids(["0C", "0D", "05", "04", "11"])
            self.engine.start()
            curses.wrapper(self._main_loop)
        except Exception:
            self._logger.exception("UI crashed")
        finally:
            self.engine.stop()

    # ------------------------------------------------------------------
    def _main_loop(self, stdscr) -> None:
        self.stdscr = stdscr
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(100)

        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
            except curses.error:
                pass
            curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE)
            curses.init_pair(3, curses.COLOR_WHITE, curses.COLOR_RED)
            curses.init_pair(4, curses.COLOR_GREEN, -1 if hasattr(curses, "COLORS") else curses.COLOR_BLACK)
            curses.init_pair(5, curses.COLOR_YELLOW, -1 if hasattr(curses, "COLORS") else curses.COLOR_BLACK)

        while True:
            try:
                stdscr.erase()
                h, w = stdscr.getmaxyx()
                if h < 12 or w < 50:
                    self._safe_addstr(stdscr, 0, 0, "Terminal too small (need 50x12). Resize.")
                    stdscr.refresh()
                    if self._read_key(stdscr) == ord("q"):
                        return
                    continue

                state = self.menu.get_current_state()
                self._render_state(stdscr, state, h, w)
                self._render_status_bar(stdscr, h, w)
                stdscr.refresh()

                c = self._read_key(stdscr)
                if c == curses.KEY_RESIZE:
                    continue
                if c == -1:
                    continue
                if self._handle_input(c, state) == "exit":
                    return
            except curses.error:
                # Transient resize or out-of-bounds write - redraw.
                continue
            except KeyboardInterrupt:
                return

    # ------------------------------------------------------------------
    def _read_key(self, stdscr):
        try:
            return stdscr.getch()
        except curses.error:
            return -1

    @staticmethod
    def _safe_addstr(win, y: int, x: int, text: str, attr=0) -> None:
        """Safe addstr that silently drops writes going off-screen
        AND refuses to touch the bottom-right cell."""
        try:
            maxy, maxx = win.getmaxyx()
        except curses.error:
            return
        if y < 0 or y >= maxy or x < 0 or x >= maxx:
            return
        avail = maxx - x - 1  # -1 to protect the bottom-right cell
        if avail <= 0:
            return
        try:
            win.addstr(y, x, text[:avail], attr)
        except curses.error:
            pass

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------
    def _render_state(self, stdscr, state: MenuState, h: int, w: int) -> None:
        if state == MenuState.MAIN_MENU:
            self._render_main(stdscr, h, w)
        elif state == MenuState.LIVE_DATA:
            self._render_live_data(stdscr, h, w)
        elif state == MenuState.DTC_READ:
            self._render_dtc(stdscr, h, w)
        elif state == MenuState.DTC_CLEAR_CONFIRM:
            self._render_dtc_clear(stdscr, h, w)
        elif state == MenuState.MODULE_SCAN:
            self._render_module_scan(stdscr, h, w)
        elif state == MenuState.BIDIR_GATE:
            self._render_bidir_gate(stdscr, h, w)
        elif state == MenuState.BIDIR_MENU:
            self._render_bidir_menu(stdscr, h, w)
        elif state == MenuState.CAPABILITY_SCAN:
            self._render_capability(stdscr, h, w)
        elif state == MenuState.DATA_LOGGING_MENU:
            self._render_data_logging(stdscr, h, w)
        elif state == MenuState.SETTINGS:
            self._render_settings(stdscr, h, w)
        else:
            self._safe_addstr(stdscr, 2, 2, f"[{state.name}] - not implemented")
            self._safe_addstr(stdscr, 4, 2, "Press Q to return.")

    def _render_header(self, stdscr, w: int, title: str) -> None:
        self._safe_addstr(
            stdscr, 0, 0, f" {title} ".center(max(0, w - 1)),
            curses.color_pair(1) | curses.A_BOLD,
        )

    def _render_main(self, stdscr, h: int, w: int) -> None:
        self._render_header(stdscr, w, "OBD2 Diagnostic & Control Platform")
        items = self._main_menu_items()
        self._render_selectable(stdscr, h, w, items)
        self._render_help(stdscr, h, w, "UP/DOWN to move - ENTER to select - Q to exit")

    def _main_menu_items(self) -> list[tuple[str, MenuState]]:
        return [
            ("Live Data", MenuState.LIVE_DATA),
            ("Data Logging Control", MenuState.DATA_LOGGING_MENU),
            ("Read DTCs", MenuState.DTC_READ),
            ("Clear DTCs", MenuState.DTC_CLEAR_CONFIRM),
            ("Module Scan", MenuState.MODULE_SCAN),
            ("Advanced Bidirectional Controls", MenuState.BIDIR_GATE),
            ("Capability Scan", MenuState.CAPABILITY_SCAN),
            ("Settings", MenuState.SETTINGS),
        ]

    def _render_selectable(self, stdscr, h: int, w: int, items: list) -> None:
        for i, entry in enumerate(items):
            label = entry[0] if isinstance(entry, tuple) else entry
            y = 2 + i
            if y >= h - 3:
                break
            if i == self.selected_idx:
                self._safe_addstr(
                    stdscr, y, 2, f"> {label}".ljust(w - 4),
                    curses.color_pair(2),
                )
            else:
                self._safe_addstr(stdscr, y, 2, f"  {label}")

    def _render_help(self, stdscr, h: int, w: int, text: str) -> None:
        self._safe_addstr(stdscr, h - 3, 2, text[: w - 4], curses.A_BOLD)

    # ------------------------------------------------------------------
    def _render_live_data(self, stdscr, h: int, w: int) -> None:
        self._render_header(stdscr, w, "Live Data")
        data = self.engine.get_latest()
        if not data:
            self._safe_addstr(stdscr, 2, 2, "Waiting for data ...")
        else:
            row = 2
            for pid, val in data.items():
                if row >= h - 3:
                    break
                self._safe_addstr(stdscr, row, 2, f"{val.name:22s}")
                self._safe_addstr(
                    stdscr, row, 26, f"{val.value} {val.unit}", curses.A_BOLD
                )
                row += 1
        self._render_help(stdscr, h, w, "Q to return")

    def _render_dtc(self, stdscr, h: int, w: int) -> None:
        self._render_header(stdscr, w, "Diagnostic Trouble Codes")
        dtcs = self.menu.context.get("dtcs")
        if dtcs is None:
            self._safe_addstr(stdscr, 2, 2, "Press R to read DTCs, Q to return.")
            return
        if not dtcs:
            self._safe_addstr(stdscr, 2, 2, "No DTCs reported.", curses.color_pair(4))
        else:
            for i, d in enumerate(dtcs):
                y = 2 + i
                if y >= h - 3:
                    break
                self._safe_addstr(stdscr, y, 2, f"{d.code}  {d.description}")
        self._render_help(stdscr, h, w, "R to refresh - Q to return")

    def _render_dtc_clear(self, stdscr, h: int, w: int) -> None:
        self._render_header(stdscr, w, "Clear DTCs")
        self._safe_addstr(stdscr, 2, 2, "This will clear emissions-related DTCs.")
        self._safe_addstr(stdscr, 3, 2, "Only proceed if you have recorded freeze frames.")
        self._safe_addstr(stdscr, 5, 2, "Press Y to confirm, Q to cancel.", curses.color_pair(5))

    def _render_module_scan(self, stdscr, h: int, w: int) -> None:
        self._render_header(stdscr, w, "Module Scan")
        mods = self.menu.context.get("modules")
        if mods is None:
            self._safe_addstr(stdscr, 2, 2, "Press R to scan, Q to return.")
            return
        if not mods:
            self._safe_addstr(stdscr, 2, 2, "No modules responded to the broadcast.")
        else:
            for i, m in enumerate(mods):
                y = 2 + i
                if y >= h - 3:
                    break
                self._safe_addstr(
                    stdscr, y, 2, f"0x{m.address:03X}  {m.name}"
                )
        self._render_help(stdscr, h, w, "R to refresh - Q to return")

    def _render_bidir_gate(self, stdscr, h: int, w: int) -> None:
        self._render_header(stdscr, w, "Advanced Bidirectional Controls")
        mode = self.gate.mode.name
        vin = self.gate.vin or "<not read>"
        reason = self.gate.reason_blocked()
        self._safe_addstr(stdscr, 2, 2, f"Mode: {mode}")
        self._safe_addstr(stdscr, 3, 2, f"VIN:  {vin}")
        self._safe_addstr(stdscr, 4, 2, f"Status: {reason}")
        self._safe_addstr(
            stdscr, 6, 2,
            "This section transmits UDS requests to the powertrain.",
            curses.color_pair(5),
        )
        self._safe_addstr(
            stdscr, 7, 2,
            "Only the 2015 GMC Sierra 1500 4.3L LV3 profile is permitted",
            curses.color_pair(5),
        )
        self._safe_addstr(
            stdscr, 8, 2,
            "on real hardware. All other vehicles are blocked by design.",
            curses.color_pair(5),
        )
        if self.gate.advanced_mode_enabled:
            self._safe_addstr(stdscr, 10, 2, "[ENTER] open bidirectional menu")
            self._safe_addstr(stdscr, 11, 2, "[D] disable advanced mode")
        else:
            self._safe_addstr(stdscr, 10, 2, "[V] read VIN from vehicle")
            self._safe_addstr(stdscr, 11, 2, "[A] enable advanced mode")
        self._render_help(stdscr, h, w, "Q to return")

    def _render_bidir_menu(self, stdscr, h: int, w: int) -> None:
        self._render_header(stdscr, w, f"Bidirectional - {BIDIR_PROFILE_OEM}")
        cmds = self.bidir_ctrl.list_available_tests(BIDIR_PROFILE_OEM)
        if not cmds:
            self._safe_addstr(stdscr, 2, 2, "No commands in this profile.")
        else:
            for i, c in enumerate(cmds):
                y = 2 + i
                if y >= h - 3:
                    break
                flag = ""
                if not (c.enabled and c.verified):
                    flag = "  [DISABLED]"
                label = f"{c.name} [{c.safety_level.name}]{flag}"
                attr = 0
                if c.safety_level == SafetyLevel.DANGER:
                    attr = curses.color_pair(3)
                if i == self.selected_idx:
                    self._safe_addstr(
                        stdscr, y, 2, f"> {label}".ljust(w - 4),
                        curses.color_pair(2),
                    )
                else:
                    self._safe_addstr(stdscr, y, 2, f"  {label}", attr)
        self._render_help(stdscr, h, w, "ENTER to execute - Q to return")

    def _render_capability(self, stdscr, h: int, w: int) -> None:
        self._render_header(stdscr, w, "Capability Scan")
        rep = self.menu.context.get("capability")
        if rep is None:
            self._safe_addstr(stdscr, 2, 2, "Press R to run capability scan, Q to return.")
            return
        self._safe_addstr(stdscr, 2, 2, f"UDS level: {rep.uds_level.name}")
        self._safe_addstr(stdscr, 3, 2, f"VIN: {rep.vin or '<unread>'}")
        self._safe_addstr(stdscr, 4, 2, f"Modules responding: {len(rep.modules)}")
        self._safe_addstr(
            stdscr, 5, 2, f"Supported PIDs: {len(rep.supported_pids)}"
        )
        if rep.supported_pids:
            pids = ", ".join(sorted(rep.supported_pids))
            self._safe_addstr(stdscr, 6, 2, pids[: w - 4])
        self._render_help(stdscr, h, w, "R to refresh - Q to return")

    def _render_data_logging(self, stdscr, h: int, w: int) -> None:
        self._render_header(stdscr, w, "Data Logging Control")
        running = self.session_mgr.running
        items = [("Stop Logging" if running else "Start Logging"), "Back"]
        for i, label in enumerate(items):
            y = 2 + i
            if i == self.selected_idx:
                self._safe_addstr(
                    stdscr, y, 2, f"> {label}".ljust(w - 4),
                    curses.color_pair(2),
                )
            else:
                self._safe_addstr(stdscr, y, 2, f"  {label}")
        if running:
            self._safe_addstr(
                stdscr, 5, 2,
                f"Status: RECORDING -> {self.session_mgr.session_id}.csv",
                curses.color_pair(4),
            )
            self._safe_addstr(
                stdscr, 6, 2, f"Rows logged: {self.session_mgr.rows_logged}"
            )
        else:
            self._safe_addstr(
                stdscr, 5, 2, "Status: STOPPED", curses.color_pair(5)
            )
        self._render_help(stdscr, h, w, "UP/DOWN to move - ENTER - Q")

    def _render_settings(self, stdscr, h: int, w: int) -> None:
        self._render_header(stdscr, w, "Settings")
        self._safe_addstr(stdscr, 2, 2, f"Simulator mode: {self.gate.simulator}")
        self._safe_addstr(stdscr, 3, 2, f"Port: {self.conn.port_name}")
        self._safe_addstr(stdscr, 4, 2, f"Protocol: {self.conn.protocol_name}")
        self._safe_addstr(stdscr, 5, 2, f"Voltage: {self.conn.voltage:.1f} V")
        self._render_help(stdscr, h, w, "Q to return")

    def _render_status_bar(self, stdscr, h: int, w: int) -> None:
        state = "CONNECTED" if self.conn.is_alive() else "DISCONNECTED"
        rec = "[REC]" if self.session_mgr.running else ""
        mode_label = {
            GateMode.UNIVERSAL: "SAFE-MODE",
            GateMode.GMC_SIERRA_2015: "GMC-ADVANCED",
            GateMode.SIMULATOR: "SIMULATOR",
        }[self.gate.mode]
        bar = (
            f" {state} {rec} | {mode_label} | {self.conn.port_name} | "
            f"{self.conn.protocol_name} | {self.conn.voltage:.1f}V "
        )
        self._safe_addstr(
            stdscr, h - 1, 0, bar.ljust(max(0, w - 1)),
            curses.color_pair(1),
        )

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------
    def _handle_input(self, c: int, state: MenuState) -> Optional[str]:
        if c == ord("q") or c == ord("Q"):
            if state == MenuState.MAIN_MENU:
                return "exit"
            self.menu.back()
            self.selected_idx = 0
            return None

        if state == MenuState.MAIN_MENU:
            return self._input_main(c)
        if state == MenuState.DTC_READ:
            return self._input_dtc_read(c)
        if state == MenuState.DTC_CLEAR_CONFIRM:
            return self._input_dtc_clear(c)
        if state == MenuState.MODULE_SCAN:
            return self._input_module_scan(c)
        if state == MenuState.BIDIR_GATE:
            return self._input_bidir_gate(c)
        if state == MenuState.BIDIR_MENU:
            return self._input_bidir_menu(c)
        if state == MenuState.CAPABILITY_SCAN:
            return self._input_capability(c)
        if state == MenuState.DATA_LOGGING_MENU:
            return self._input_data_logging(c)
        return None

    # --- main ---------------------------------------------------------
    def _input_main(self, c: int) -> Optional[str]:
        items = self._main_menu_items()
        if c == curses.KEY_UP:
            self.selected_idx = max(0, self.selected_idx - 1)
        elif c == curses.KEY_DOWN:
            self.selected_idx = min(len(items) - 1, self.selected_idx + 1)
        elif c in (10, 13, curses.KEY_ENTER):
            _, target = items[self.selected_idx]
            self.menu.navigate(target)
            self.selected_idx = 0
        return None

    # --- DTC ----------------------------------------------------------
    def _input_dtc_read(self, c: int) -> None:
        if c in (ord("r"), ord("R")):
            self.engine.pause()
            try:
                dtcs = self.dtc_mgr.read_stored()
            finally:
                self.engine.resume()
            self.menu.context["dtcs"] = dtcs
        return None

    def _input_dtc_clear(self, c: int) -> None:
        if c in (ord("y"), ord("Y")):
            self.engine.pause()
            try:
                ok = self.dtc_mgr.clear_codes()
            finally:
                self.engine.resume()
            self._popup("Clear DTCs", "Cleared." if ok else "Clear failed.")
            self.menu.back()
            self.selected_idx = 0
        return None

    # --- module scan --------------------------------------------------
    def _input_module_scan(self, c: int) -> None:
        if c in (ord("r"), ord("R")):
            self.engine.pause()
            try:
                mods = self.module_scanner.scan_all()
            finally:
                self.engine.resume()
            self.menu.context["modules"] = mods
        return None

    # --- bidir gate ---------------------------------------------------
    def _input_bidir_gate(self, c: int) -> None:
        if c in (ord("v"), ord("V")):
            self.engine.pause()
            try:
                vin = self.cap_detector.detect_vin()
            finally:
                self.engine.resume()
            self.gate.record_vin(vin)
            return None
        if c in (ord("a"), ord("A")):
            if not self.gate.vin_decision or not self.gate.vin_decision.matches_gmc_sierra_2015_lv3:
                if not self.gate.simulator:
                    self._popup(
                        "Blocked",
                        "VIN does not match 2015 GMC Sierra 4.3L LV3.\n"
                        "Advanced mode is permanently disabled for this\n"
                        "vehicle. Run in simulator mode to sandbox.",
                    )
                    return None
            if not self._confirm(
                "Advanced Mode will transmit UDS to the powertrain.\n"
                "Are you sure? (y/n)"
            ):
                return None
            if self.gate.enable_advanced_mode():
                self._popup("Advanced Mode", "Unlocked.")
            else:
                self._popup("Advanced Mode", "Refused by safety gate.")
            return None
        if c in (ord("d"), ord("D")):
            self.gate.disable_advanced_mode()
            self._popup("Advanced Mode", "Disabled.")
            return None
        if c in (10, 13, curses.KEY_ENTER):
            if self.gate.advanced_mode_enabled:
                self.menu.navigate(MenuState.BIDIR_MENU)
                self.selected_idx = 0
        return None

    # --- bidir menu ---------------------------------------------------
    def _input_bidir_menu(self, c: int) -> None:
        cmds = self.bidir_ctrl.list_available_tests(BIDIR_PROFILE_OEM)
        if not cmds:
            return None
        if c == curses.KEY_UP:
            self.selected_idx = max(0, self.selected_idx - 1)
        elif c == curses.KEY_DOWN:
            self.selected_idx = min(len(cmds) - 1, self.selected_idx + 1)
        elif c in (10, 13, curses.KEY_ENTER):
            self._execute_bidir(cmds[self.selected_idx])
        return None

    # --- capability ---------------------------------------------------
    def _input_capability(self, c: int) -> None:
        if c in (ord("r"), ord("R")):
            self.engine.pause()
            try:
                rep = self.cap_detector.build_report()
            finally:
                self.engine.resume()
            self.menu.context["capability"] = rep
        return None

    # --- data logging -------------------------------------------------
    def _input_data_logging(self, c: int) -> None:
        if c == curses.KEY_UP:
            self.selected_idx = max(0, self.selected_idx - 1)
        elif c == curses.KEY_DOWN:
            self.selected_idx = min(1, self.selected_idx + 1)
        elif c in (10, 13, curses.KEY_ENTER):
            if self.selected_idx == 0:
                if self.session_mgr.running:
                    summary = self.session_mgr.end_session()
                    self._popup(
                        "Data Logging",
                        f"Stopped.\n{summary.rows_logged} rows written.",
                    )
                else:
                    self.session_mgr.start_session({"note": "manual start"})
                    self._popup("Data Logging", "Recording started.")
                self.menu.back()
                self.selected_idx = 0
            else:
                self.menu.back()
                self.selected_idx = 0
        return None

    # ------------------------------------------------------------------
    # Bidirectional execution
    # ------------------------------------------------------------------
    def _execute_bidir(self, cmd: BidirCommand) -> None:
        if not cmd.enabled or not cmd.verified:
            self._popup(
                "Disabled",
                f"{cmd.name} is not marked enabled+verified in the\n"
                "vehicle profile. It will not be transmitted.",
            )
            return

        needs_confirm = cmd.safety_level in (
            SafetyLevel.CAUTION, SafetyLevel.DANGER
        )
        if needs_confirm:
            danger = cmd.safety_level == SafetyLevel.DANGER
            prompt = (
                f"{cmd.safety_level.name}: {cmd.name}\n"
                f"{cmd.description}\n\n"
                f"{'!! This command can damage hardware if misused. !!' if danger else ''}\n"
                "Proceed? (y/n)"
            )
            if not self._confirm(prompt):
                return

        if not self.engine.pause():
            self._popup("Busy", "Live data poller did not reach idle in time.")
            return
        try:
            try:
                result: TestResult = self.bidir_ctrl.execute_test(
                    BIDIR_PROFILE_OEM, cmd, confirm=True
                )
                self._show_result(cmd, result)
            except SafetyInterlock as exc:
                self._popup("Blocked by safety gate", str(exc))
            except Exception as exc:
                self._logger.exception("bidir execution failed")
                self._popup("Error", f"{exc}")
        finally:
            # Defensive: ask the controller to abort just in case the
            # code path above returned without its own finally firing.
            try:
                self.bidir_ctrl.abort_test(cmd)
            except Exception:
                pass
            self.engine.resume()

    def _show_result(self, cmd: BidirCommand, result: TestResult) -> None:
        lines = [f"{cmd.name}", f"elapsed: {result.elapsed_ms:.1f} ms"]
        if result.success:
            lines.append("Result: POSITIVE")
        else:
            lines.append("Result: REJECTED")
            if result.nrc is not None:
                lines.append(f"NRC: 0x{result.nrc:02X}")
            if result.notes:
                lines.append(result.notes)
        self._popup("Test Result", "\n".join(lines))

    # ------------------------------------------------------------------
    # Popups
    # ------------------------------------------------------------------
    def _confirm(self, message: str) -> bool:
        return bool(self._modal(message, wait_keys=(ord("y"), ord("n"), 27)))

    def _popup(self, title: str, message: str) -> None:
        self._modal(message, wait_keys=None, title=title)

    def _modal(
        self,
        message: str,
        wait_keys: Optional[tuple] = None,
        title: str = "",
    ):
        if self.stdscr is None:
            return False
        self.stdscr.nodelay(False)
        try:
            h, w = self.stdscr.getmaxyx()
            lines = message.split("\n")
            box_h = len(lines) + 4
            box_w = max(max(len(l) for l in lines), len(title) + 4) + 4
            box_h = min(box_h, max(5, h - 2))
            box_w = min(box_w, max(20, w - 2))
            y = max(0, (h - box_h) // 2)
            x = max(0, (w - box_w) // 2)
            try:
                win = curses.newwin(box_h, box_w, y, x)
            except curses.error:
                return False
            win.box()
            if title:
                try:
                    win.addstr(0, 2, f" {title} ", curses.A_BOLD)
                except curses.error:
                    pass
            for i, line in enumerate(lines):
                if i + 2 >= box_h - 1:
                    break
                try:
                    win.addstr(i + 2, 2, line[: box_w - 4])
                except curses.error:
                    pass
            try:
                win.addstr(box_h - 2, 2, "Press any key ..." if wait_keys is None else "y / n ")
            except curses.error:
                pass
            win.refresh()
            while True:
                try:
                    c = win.getch()
                except curses.error:
                    c = -1
                if wait_keys is None:
                    if c != -1:
                        return True
                else:
                    if c in (ord("y"), ord("Y")):
                        return True
                    if c in (ord("n"), ord("N"), 27, ord("q"), ord("Q")):
                        return False
        finally:
            try:
                self.stdscr.nodelay(True)
            except curses.error:
                pass
            try:
                self.stdscr.touchwin()
            except curses.error:
                pass
