from dataclasses import dataclass
from datetime import datetime

@dataclass
class Anomaly:
    timestamp: datetime
    pid: str
    value: float
    expected_range: tuple
    severity: str

@dataclass
class StatsReport:
    stats: dict

@dataclass
class SessionData:
    session_id: str
    start_time: datetime
    end_time: datetime
    vehicle_info: dict
    rows: list[dict]
    dtcs_captured: list

class OfflineAnalyzer:
    def __init__(self, session_dir: str):
        self.session_dir = session_dir
        
    def load_session(self, csv_path: str) -> SessionData:
        return SessionData("test", datetime.now(), datetime.now(), {}, [], [])
        
    def compute_statistics(self, data: SessionData) -> StatsReport:
        return StatsReport({})
        
    def detect_anomalies(self, data: SessionData, threshold_sigma: float = 3.0) -> list[Anomaly]:
        return []
        
    def generate_report(self, data: SessionData, format: str = 'text') -> str:
        return "Report"
        
    def export_report(self, report: str, path: str):
        pass
