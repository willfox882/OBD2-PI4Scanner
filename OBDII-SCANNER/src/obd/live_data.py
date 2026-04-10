"""
LiveDataEngine - polls a set of Mode 01 PIDs in a background thread.

Contract with the rest of the system:
  * ``pause()`` MUST block until any in-flight query has completed and
    the poller is guaranteed to make no further queries until
    ``resume()``.  This is the mechanism that prevents a
    bidirectional UDS command from racing the live poller on the
    shared ELM327 serial port.
  * ``resume()`` is cheap; it just re-arms the loop.
  * ``stop()`` terminates the thread entirely and joins it.

The poller always acquires the connection lock *implicitly* because
every ``conn.query()`` call is itself lock-guarded.  The additional
pause/resume Event prevents the poller from even queuing queries
during a bidirectional test, which keeps worst-case latency bounded.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

from ..core.connection import OBDConnection
from .pids import DecodedValue, PIDRegistry


class LiveDataEngine:
    def __init__(
        self,
        conn: OBDConnection,
        registry: PIDRegistry,
        session_mgr=None,
        poll_interval: float = 0.1,
    ) -> None:
        self.conn = conn
        self.registry = registry
        self.session_mgr = session_mgr
        self.poll_interval = poll_interval

        self._active_pids: List[str] = []
        self._data: Dict[str, DecodedValue] = {}
        self._lock = threading.Lock()

        self._stop_event = threading.Event()
        self._allowed_event = threading.Event()
        self._allowed_event.set()
        self._idle_event = threading.Event()
        self._idle_event.set()

        self._thread: Optional[threading.Thread] = None
        self._logger = logging.getLogger("LiveDataEngine")

    # ------------------------------------------------------------------
    def set_active_pids(self, pids: List[str]) -> None:
        with self._lock:
            self._active_pids = list(pids)
        self._logger.info("Active PIDs: %s", pids)

    def get_latest(self) -> Dict[str, DecodedValue]:
        with self._lock:
            return dict(self._data)

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._allowed_event.set()
        self._thread = threading.Thread(
            target=self._poll_loop, name="LiveDataEngine", daemon=True
        )
        self._thread.start()
        self._logger.info("Live data thread started")

    def stop(self) -> None:
        self._stop_event.set()
        self._allowed_event.set()  # unblock any wait
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._logger.info("Live data thread stopped")

    def pause(self, timeout: float = 5.0) -> bool:
        """Block further polling and wait for any in-flight query to
        finish.  Returns True on success, False if the poller did not
        reach idle before the timeout."""
        self._allowed_event.clear()
        reached = self._idle_event.wait(timeout=timeout)
        if not reached:
            self._logger.error("pause() timed out waiting for idle")
        return reached

    def resume(self) -> None:
        self._allowed_event.set()

    # ------------------------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            # Honour pause requests before touching the bus.
            if not self._allowed_event.is_set():
                self._idle_event.set()
                self._allowed_event.wait(timeout=0.5)
                continue

            if not self.conn.is_alive():
                self._idle_event.set()
                time.sleep(0.5)
                continue

            with self._lock:
                pids = list(self._active_pids)
            if not pids:
                self._idle_event.set()
                time.sleep(0.25)
                continue

            # Mark ourselves busy before the first query of this cycle.
            self._idle_event.clear()
            try:
                for pid in pids:
                    if self._stop_event.is_set() or not self._allowed_event.is_set():
                        break
                    resp = self.conn.query(f"01{pid}")
                    if not resp.ok:
                        continue
                    decoded = self.registry.decode(pid, resp.joined_hex())
                    if decoded is None:
                        continue
                    with self._lock:
                        self._data[pid] = decoded
                    if self.session_mgr is not None and getattr(
                        self.session_mgr, "running", False
                    ):
                        try:
                            self.session_mgr.log_row({pid: decoded})
                        except Exception as exc:
                            self._logger.error("session log failed: %s", exc)
                    time.sleep(self.poll_interval)
            finally:
                self._idle_event.set()
