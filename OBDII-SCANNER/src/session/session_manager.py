import os
import csv
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from ..obd.pids import DecodedValue
from ..obd.dtc import DTCEntry

@dataclass
class SessionSummary:
    session_id: str
    duration: timedelta
    rows_logged: int
    dtcs_found: int
    file_path: str

class SessionManager:
    def __init__(self, log_dir='./logs', flush_interval=5):
        self.log_dir = log_dir
        self.flush_interval = flush_interval
        self.session_id = None
        self.file = None
        self.writer = None
        self.dtc_file = None
        self.dtc_writer = None
        self.start_time = None
        self.rows_logged = 0
        self.dtcs_found = 0
        self.running = False
        self.thread = None
        self.lock = threading.Lock()
        
        os.makedirs(log_dir, exist_ok=True)
        
    def start_session(self, vehicle_info: dict) -> str:
        with self.lock:
            self.session_id = f"ses_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            self.start_time = datetime.now()
            
            filepath = os.path.join(self.log_dir, f"{self.session_id}.csv")
            self.file = open(filepath, 'w', newline='')
            self.writer = csv.writer(self.file)
            self.writer.writerow(["timestamp", "session_id", "pid_id", "pid_name", "raw_value", "decoded_value", "unit", "is_suspect"])
            
            dtc_filepath = os.path.join(self.log_dir, f"{self.session_id}_dtcs.csv")
            self.dtc_file = open(dtc_filepath, 'w', newline='')
            self.dtc_writer = csv.writer(self.dtc_file)
            self.dtc_writer.writerow(["timestamp", "session_id", "module", "code", "description", "status"])
            
            self.running = True
            self.thread = threading.Thread(target=self._flush_timer, daemon=True)
            self.thread.start()
            
            return self.session_id
            
    def log_row(self, data: dict[str, DecodedValue]):
        with self.lock:
            if not self.writer or not self.running: return
            try:
                for pid, val in data.items():
                    raw_repr = val.raw.hex() if isinstance(val.raw, (bytes, bytearray)) else str(val.raw)
                    self.writer.writerow([
                        val.timestamp.isoformat(),
                        self.session_id,
                        val.pid_id,
                        val.name,
                        raw_repr,
                        str(val.value),
                        val.unit,
                        str(val.is_suspect).lower()
                    ])
                    self.rows_logged += 1
            except OSError as e:
                # Handle "No space left on device" (Errno 28)
                self.running = False
                print(f"CRITICAL: Logging stopped. Disk full or write error: {e}")
                
    def log_dtc_event(self, dtcs: list[DTCEntry]):
        with self.lock:
            if not self.dtc_writer or not self.running: return
            try:
                for dtc in dtcs:
                    self.dtc_writer.writerow([
                        dtc.timestamp.isoformat(),
                        self.session_id,
                        dtc.module,
                        dtc.code,
                        dtc.description,
                        dtc.status.name
                    ])
                    self.dtcs_found += 1
                self.dtc_file.flush()
                os.fsync(self.dtc_file.fileno())
            except OSError:
                self.running = False
            
    def end_session(self) -> SessionSummary:
        with self.lock:
            self.running = False
            if self.file:
                self.file.flush()
                os.fsync(self.file.fileno())
                self.file.close()
            if self.dtc_file:
                self.dtc_file.flush()
                os.fsync(self.dtc_file.fileno())
                self.dtc_file.close()
                
            duration = datetime.now() - self.start_time if self.start_time else timedelta(0)
            
            summary = SessionSummary(
                self.session_id,
                duration,
                self.rows_logged,
                self.dtcs_found,
                os.path.join(self.log_dir, f"{self.session_id}.csv")
            )
            
            self.session_id = None
            self.file = None
            self.writer = None
            self.dtc_file = None
            self.dtc_writer = None
            
            return summary
            
    def get_active_session(self) -> str | None:
        return self.session_id
        
    def _flush_timer(self):
        while True:
            # Check the stop flag frequently so shutdown is snappy.
            for _ in range(int(self.flush_interval * 10)):
                if not self.running:
                    return
                time.sleep(0.1)
            if not self.running:
                return
            file_to_sync = None
            dtc_file_to_sync = None
            with self.lock:
                if self.file and not self.file.closed:
                    self.file.flush()
                    file_to_sync = self.file.fileno()
                if self.dtc_file and not self.dtc_file.closed:
                    self.dtc_file.flush()
                    dtc_file_to_sync = self.dtc_file.fileno()
            
            # fsync outside the lock to avoid blocking other threads
            if file_to_sync is not None:
                try:
                    os.fsync(file_to_sync)
                except OSError:
                    pass
            if dtc_file_to_sync is not None:
                try:
                    os.fsync(dtc_file_to_sync)
                except OSError:
                    pass
