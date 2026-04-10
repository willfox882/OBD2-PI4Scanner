/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { CodeBlock } from './components/CodeBlock';
import { Terminal, FileCode, FolderTree, Info } from 'lucide-react';

const pythonCode = `import obd
import time
import csv
import sys
import threading
import queue
from datetime import datetime
from obd import OBDStatus

# --- Configuration ---
OBD_PORT = None 
BAUDRATE = 115200

# --- Analysis Engine Parameters ---
CAT_TEMP_WARNING_THRESHOLD = 850.0 # Celsius
O2_LEAN_THRESHOLD = 0.4 # Volts
O2_RICH_THRESHOLD = 0.6 # Volts
O2_SWITCH_TIME_WINDOW = 10.0 # Seconds
O2_MIN_SWITCHES = 5 # Minimum switches required in the time window
STEADY_RPM_MIN = 1500
STEADY_RPM_MAX = 2500
BUFFER_MAX_AGE = 15.0 # Seconds

class DiagnosticAgent:
    def __init__(self):
        self.connection = None
        self.is_connected = False
        self.running = True
        self.log_filename = f"obd_inspection_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        self.lock = threading.Lock()
        
        self.o2_b1s1_buffer = []
        self.o2_b1s2_buffer = []
        self.rpm_buffer = []
        
        self.current_data = {
            "rpm": None,
            "cat_temp": None,
            "o2_b1s1": None,
            "o2_b1s2": None,
            "ltft": None
        }
        
        self.status = {
            "cat_temp_warning": False,
            "o2_upstream_lazy": False,
            "cat_efficiency_bad": False
        }
        
        self.log_queue = queue.Queue(maxsize=1000)
        self.logger_thread = threading.Thread(target=self._logger_worker, daemon=True)
        self.logger_thread.start()

    def _logger_worker(self):
        try:
            with open(self.log_filename, mode='w', newline='') as file:
                writer = csv.writer(file)
                writer.writerow(["Timestamp", "RPM", "Cat_Temp_B1S1_C", "O2_B1S1_V", "O2_B1S2_V", "LTFT1_%"])
                while self.running or not self.log_queue.empty():
                    try:
                        data = self.log_queue.get(timeout=0.5)
                        if data is None:
                            break
                        writer.writerow(data)
                        file.flush()
                    except queue.Empty:
                        continue
                    except Exception:
                        pass
        except Exception as e:
            print(f"[-] Logger failed to start: {e}")

    def connect(self):
        sys.stdout.write("\\033[H\\033[J")
        print("[*] Attempting to connect to OBD2 adapter...")
        try:
            if self.connection:
                self.connection.close()
            self.connection = obd.Async(portstr=OBD_PORT, baudrate=BAUDRATE, fast=False, delay_cmds=0.1)
            if self.connection.status() == OBDStatus.CAR_CONNECTED:
                print("[+] Successfully connected to vehicle!")
                self.is_connected = True
                self.setup_async_tracking()
                self.connection.start()
            else:
                print("[-] Failed to connect. Retrying in 5 seconds...")
                self.is_connected = False
        except Exception as e:
            print(f"[-] Connection error: {e}")
            self.is_connected = False

    def setup_async_tracking(self):
        self.connection.watch(obd.commands.RPM, callback=self.new_rpm)
        self.connection.watch(obd.commands.CATALYST_TEMP_B1S1, callback=self.new_cat_temp)
        self.connection.watch(obd.commands.O2_B1S1, callback=self.new_o2_b1s1)
        self.connection.watch(obd.commands.O2_B1S2, callback=self.new_o2_b1s2)
        self.connection.watch(obd.commands.LONG_TERM_FUEL_TRIM_1, callback=self.new_ltft)

    def new_rpm(self, r):
        if not r.is_null():
            val = r.value.magnitude
            with self.lock:
                self.current_data["rpm"] = val
                self.rpm_buffer.append((time.monotonic(), val))

    def new_cat_temp(self, r):
        if not r.is_null():
            val = r.value.magnitude
            with self.lock:
                self.current_data["cat_temp"] = val
                self.status["cat_temp_warning"] = (val > CAT_TEMP_WARNING_THRESHOLD)

    def new_o2_b1s1(self, r):
        if not r.is_null():
            val = r.value.magnitude
            with self.lock:
                self.current_data["o2_b1s1"] = val
                self.o2_b1s1_buffer.append((time.monotonic(), val))

    def new_o2_b1s2(self, r):
        if not r.is_null():
            val = r.value.magnitude
            with self.lock:
                self.current_data["o2_b1s2"] = val
                self.o2_b1s2_buffer.append((time.monotonic(), val))

    def new_ltft(self, r):
        if not r.is_null():
            with self.lock:
                self.current_data["ltft"] = r.value.magnitude

    def clean_buffers(self):
        now = time.monotonic()
        with self.lock:
            self.rpm_buffer = [(t, v) for t, v in self.rpm_buffer if now - t <= BUFFER_MAX_AGE]
            self.o2_b1s1_buffer = [(t, v) for t, v in self.o2_b1s1_buffer if now - t <= BUFFER_MAX_AGE]
            self.o2_b1s2_buffer = [(t, v) for t, v in self.o2_b1s2_buffer if now - t <= BUFFER_MAX_AGE]

    def analyze_o2_switch_frequency(self):
        now = time.monotonic()
        with self.lock:
            recent_rpms = [v for t, v in self.rpm_buffer if now - t <= O2_SWITCH_TIME_WINDOW]
            recent_o2 = [v for t, v in self.o2_b1s1_buffer if now - t <= O2_SWITCH_TIME_WINDOW]

        if not recent_rpms or not recent_o2:
            return

        avg_rpm = sum(recent_rpms) / len(recent_rpms)
        if not (STEADY_RPM_MIN <= avg_rpm <= STEADY_RPM_MAX):
            return

        switches = 0
        current_state = None
        
        for volts in recent_o2:
            if volts < O2_LEAN_THRESHOLD:
                if current_state == 'rich':
                    switches += 1
                current_state = 'lean'
            elif volts > O2_RICH_THRESHOLD:
                if current_state == 'lean':
                    switches += 1
                current_state = 'rich'
                
        with self.lock:
            if switches < O2_MIN_SWITCHES and len(recent_o2) > 10:
                self.status["o2_upstream_lazy"] = True
            else:
                self.status["o2_upstream_lazy"] = False

    def analyze_cat_efficiency(self):
        now = time.monotonic()
        with self.lock:
            recent_b1s2 = [v for t, v in self.o2_b1s2_buffer if now - t <= O2_SWITCH_TIME_WINDOW]
        
        if not recent_b1s2:
            return
            
        switches = 0
        current_state = None
        
        for volts in recent_b1s2:
            if volts < O2_LEAN_THRESHOLD:
                if current_state == 'rich':
                    switches += 1
                current_state = 'lean'
            elif volts > O2_RICH_THRESHOLD:
                if current_state == 'lean':
                    switches += 1
                current_state = 'rich'
                
        with self.lock:
            if switches > 3 and len(recent_b1s2) > 10:
                self.status["cat_efficiency_bad"] = True
            else:
                self.status["cat_efficiency_bad"] = False

    def queue_log_data(self):
        with self.lock:
            rpm = self.current_data["rpm"]
            cat_temp = self.current_data["cat_temp"]
            o2_b1s1 = self.current_data["o2_b1s1"]
            o2_b1s2 = self.current_data["o2_b1s2"]
            ltft = self.current_data["ltft"]
            
        row = [
            datetime.now().isoformat(),
            f"{rpm:.2f}" if rpm is not None else "",
            f"{cat_temp:.2f}" if cat_temp is not None else "",
            f"{o2_b1s1:.3f}" if o2_b1s1 is not None else "",
            f"{o2_b1s2:.3f}" if o2_b1s2 is not None else "",
            f"{ltft:.2f}" if ltft is not None else ""
        ]
        try:
            self.log_queue.put_nowait(row)
        except queue.Full:
            pass

    def print_status_report(self):
        sys.stdout.write("\\033[H\\033[J")
        
        with self.lock:
            rpm = self.current_data["rpm"]
            cat_temp = self.current_data["cat_temp"]
            o2_b1s1 = self.current_data["o2_b1s1"]
            o2_b1s2 = self.current_data["o2_b1s2"]
            ltft = self.current_data["ltft"]
            cat_warn = self.status["cat_temp_warning"]
            o2_lazy = self.status["o2_upstream_lazy"]
            cat_bad = self.status["cat_efficiency_bad"]

        print("="*50)
        print("   PRE-PURCHASE INSPECTION AGENT - LIVE DATA")
        print("="*50)
        
        print(f"RPM: {rpm:.0f}" if rpm is not None else "RPM: WAIT")
        print(f"LTFT1: {ltft:.2f}%" if ltft is not None else "LTFT1: WAIT")
        print("-" * 50)
        
        if cat_temp is not None:
            if cat_warn:
                print(f"\\033[91m[FAIL] Catalyst Temp: {cat_temp:.1f}°C (OVERHEATING/CLOGGED)\\033[0m")
            else:
                print(f"\\033[92m[PASS] Catalyst Temp: {cat_temp:.1f}°C (Normal)\\033[0m")
        else:
            print("[WAIT] Catalyst Temp: Waiting for data...")
            
        if o2_b1s1 is not None:
            print(f"O2 B1S1 (Upstream) Voltage: {o2_b1s1:.3f}V")
            if o2_lazy:
                print("\\033[91m[FAIL] Upstream O2 Sensor: LAZY/FAILING (Slow switching)\\033[0m")
            else:
                print("\\033[92m[PASS] Upstream O2 Sensor: ACTIVE (Good switching)\\033[0m")
        else:
            print("[WAIT] Upstream O2 Sensor: Waiting for data...")
            
        if o2_b1s2 is not None:
            print(f"O2 B1S2 (Downstream) Voltage: {o2_b1s2:.3f}V")
            if cat_bad:
                print("\\033[91m[FAIL] Catalytic Converter: BAD CAT (Downstream mimicking Upstream)\\033[0m")
            else:
                print("\\033[92m[PASS] Catalytic Converter: EFFICIENT (Steady downstream voltage)\\033[0m")
        else:
            print("[WAIT] Catalytic Converter: Waiting for data...")
            
        print("="*50)
        print("Press Ctrl+C to stop logging and exit.")

    def run(self):
        try:
            while self.running:
                if not self.is_connected or (self.connection and self.connection.status() != OBDStatus.CAR_CONNECTED):
                    self.connect()
                    if not self.is_connected:
                        time.sleep(5)
                        continue
                
                self.clean_buffers()
                self.analyze_o2_switch_frequency()
                self.analyze_cat_efficiency()
                
                self.queue_log_data()
                self.print_status_report()
                
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            print(f"\\n[-] Fatal error: {e}")
        finally:
            self.shutdown()

    def shutdown(self):
        print("\\n[*] Shutting down...")
        self.running = False
        try:
            self.log_queue.put_nowait(None)
        except queue.Full:
            pass
        if self.connection:
            self.connection.stop()
            self.connection.close()
        self.logger_thread.join(timeout=2.0)
        print("[*] Inspection stopped. Log saved.")

if __name__ == "__main__":
    agent = DiagnosticAgent()
    agent.run()
`;

const requirementsCode = `python-obd==0.7.1
pyserial==3.5
pandas>=1.5.0
matplotlib>=3.5.0
numpy>=1.21.0`;

const analyzeLogCode = `import os
import glob
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from datetime import datetime

plt.switch_backend('Agg')

LOG_DIR = 'logs'
REPORT_DIR = 'reports'

def find_latest_log():
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)
    
    files = glob.glob(os.path.join(LOG_DIR, '*.csv'))
    if not files:
        files = glob.glob('*.csv')
        
    if not files:
        raise FileNotFoundError("No CSV log files found in /logs/ or current directory.")
        
    return max(files, key=os.path.getctime)

def load_and_clean_data(filepath):
    df = pd.read_csv(filepath)
    df['Timestamp'] = pd.to_datetime(df['Timestamp'], errors='coerce')
    df = df.dropna(subset=['Timestamp'])
    df = df.sort_values('Timestamp').reset_index(drop=True)
    
    numeric_cols = ['RPM', 'Cat_Temp_B1S1_C', 'O2_B1S1_V', 'O2_B1S2_V', 'LTFT1_%']
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
            
    df = df.ffill().bfill()
    return df

def detect_anomalies(df):
    anomalies = []
    
    if 'Cat_Temp_B1S1_C' in df.columns:
        max_temp = df['Cat_Temp_B1S1_C'].max()
        if max_temp > 850.0:
            anomalies.append(f"CRITICAL: Catalyst Overheating detected (Max: {max_temp:.1f}°C > 850°C)")
            
    if 'LTFT1_%' in df.columns:
        max_ltft = df['LTFT1_%'].abs().max()
        if max_ltft > 15.0:
            anomalies.append(f"WARNING: Abnormal Long-Term Fuel Trim detected (Max Abs: {max_ltft:.1f}% > 15%)")
            
    if 'O2_B1S1_V' in df.columns and 'RPM' in df.columns:
        steady_rpm_mask = (df['RPM'] >= 1500) & (df['RPM'] <= 2500)
        steady_df = df[steady_rpm_mask]
        
        if len(steady_df) > 50:
            lean_to_rich = (steady_df['O2_B1S1_V'] < 0.4).astype(int).diff() == 1
            rich_to_lean = (steady_df['O2_B1S1_V'] > 0.6).astype(int).diff() == 1
            switches = (lean_to_rich | rich_to_lean).sum()
            
            time_span_seconds = (steady_df['Timestamp'].max() - steady_df['Timestamp'].min()).total_seconds()
            if time_span_seconds > 0:
                switches_per_10s = (switches / time_span_seconds) * 10
                if switches_per_10s < 5:
                    anomalies.append(f"WARNING: Lazy Upstream O2 Sensor (Switches/10s: {switches_per_10s:.1f} < 5)")
                    
    if 'O2_B1S1_V' in df.columns and 'O2_B1S2_V' in df.columns:
        up_var = df['O2_B1S1_V'].var()
        down_var = df['O2_B1S2_V'].var()
        
        if down_var > 0.05 and down_var > (up_var * 0.5):
            anomalies.append(f"CRITICAL: Catalytic Converter Inefficiency (Downstream variance {down_var:.3f} is too high/mimicking upstream)")
            
    if df.isnull().any().any():
        anomalies.append("WARNING: Missing or malformed data detected and interpolated.")
        
    return anomalies

def generate_report(df, filepath, anomalies):
    if not os.path.exists(REPORT_DIR):
        os.makedirs(REPORT_DIR)
        
    report_filename = f"analysis_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    report_path = os.path.join(REPORT_DIR, report_filename)
    
    with PdfPages(report_path) as pdf:
        fig = plt.figure(figsize=(10, 8))
        plt.axis('off')
        plt.title("OBD2 Pre-Purchase Inspection - Vehicle Health Summary", fontsize=16, fontweight='bold')
        
        summary_text = f"Log File Analyzed: {os.path.basename(filepath)}\\n"
        summary_text += f"Analysis Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\\n"
        summary_text += f"Total Data Points: {len(df)}\\n"
        summary_text += f"Duration: {(df['Timestamp'].max() - df['Timestamp'].min()).total_seconds():.1f} seconds\\n\\n"
        
        summary_text += "--- SUMMARY STATISTICS ---\\n"
        stats = df.describe().loc[['mean', 'min', 'max', 'std']]
        summary_text += stats.to_string() + "\\n\\n"
        
        summary_text += "--- ANOMALY DETECTION ---\\n"
        if not anomalies:
            summary_text += "No critical anomalies detected. Vehicle health appears normal.\\n"
        else:
            for a in anomalies:
                summary_text += f"- {a}\\n"
                
        plt.text(0.05, 0.95, summary_text, fontsize=10, family='monospace', va='top', ha='left', wrap=True)
        pdf.savefig(fig)
        plt.close()
        
        cols_to_plot = [c for c in ['RPM', 'Cat_Temp_B1S1_C', 'O2_B1S1_V', 'O2_B1S2_V', 'LTFT1_%'] if c in df.columns]
        
        fig, axes = plt.subplots(len(cols_to_plot), 1, figsize=(12, 3 * len(cols_to_plot)), sharex=True)
        if len(cols_to_plot) == 1:
            axes = [axes]
            
        for ax, col in zip(axes, cols_to_plot):
            ax.plot(df['Timestamp'], df[col], label=col, color='blue' if 'O2' not in col else ('green' if 'S1' in col else 'red'))
            ax.set_ylabel(col)
            ax.grid(True, linestyle='--', alpha=0.7)
            ax.legend(loc='upper right')
            
            if col == 'Cat_Temp_B1S1_C':
                ax.axhline(850, color='r', linestyle='--', alpha=0.5)
            elif col == 'O2_B1S1_V' or col == 'O2_B1S2_V':
                ax.axhline(0.4, color='orange', linestyle='--', alpha=0.5)
                ax.axhline(0.6, color='orange', linestyle='--', alpha=0.5)
                ax.set_ylim(0, 1.2)
                
        axes[-1].set_xlabel('Time')
        plt.suptitle('OBD2 Telemetry Signals', fontsize=14)
        plt.tight_layout()
        pdf.savefig(fig)
        plt.close()
        
    return report_path

def main():
    try:
        latest_log = find_latest_log()
        df = load_and_clean_data(latest_log)
        anomalies = detect_anomalies(df)
        report_path = generate_report(df, latest_log, anomalies)
        print(f"Analysis complete. Report saved to: {report_path}")
    except Exception as e:
        print(f"Analysis failed: {e}")

if __name__ == "__main__":
    main()
\`;

const readmeCode = `# OBD2 Pre-Purchase Inspection Agent

A Python-based diagnostic tool designed for Raspberry Pi to perform in-depth analysis of live vehicle data. This tool acts as an "Inspection Agent" to find hidden problems before purchasing a used vehicle, with a focus on GMC Sierra 1500 and Ford trucks.

## Features
- **Auto-Protocol Detection**: Connects automatically using the OBDLink EX USB adapter.
- **Live Data Monitoring**: Asynchronously polls RPM, Catalyst Temp, O2 Sensor Voltages, and Long Term Fuel Trim.
- **Analysis Engine**:
  - **Catalyst Overheating**: Flags if Catalyst Temp exceeds 850°C.
  - **Lazy O2 Sensor**: Analyzes upstream O2 sensor switch frequency to detect failing sensors.
  - **Catalytic Converter Efficiency**: Compares upstream and downstream O2 sensors to detect a "dead" cat.
- **Logging**: Generates a timestamped CSV file of all polled data.
- **Live Terminal UI**: Displays a Red/Green status report directly in the terminal.

## Hardware Requirements
- Raspberry Pi 4 (running Raspberry Pi OS Lite)
- OBDLink EX USB adapter

## Installation

1. Clone this repository to your Raspberry Pi.
2. Install the required Python packages:
   \`\`\`bash
   pip install -r requirements.txt
   \`\`\`

## Usage

1. Connect the OBDLink EX adapter to the vehicle's OBD2 port and the Raspberry Pi's USB port.
2. Turn the vehicle's ignition to ON (or start the engine for live data).
3. Run the script:
   \`\`\`bash
   python main.py
   \`\`\`
4. The tool will automatically connect, start logging to a CSV file, and display the live status report in the terminal.
`;

export default function App() {
  return (
    <div className="min-h-screen bg-gray-50 text-gray-900 font-sans">
      <header className="bg-slate-900 text-white py-12 px-6 shadow-md">
        <div className="max-w-5xl mx-auto">
          <div className="flex items-center gap-3 mb-4">
            <Terminal className="w-8 h-8 text-blue-400" />
            <h1 className="text-3xl font-bold tracking-tight">OBD2 Pre-Purchase Inspection Agent</h1>
          </div>
          <p className="text-slate-300 text-lg max-w-2xl leading-relaxed">
            A Python-based diagnostic architecture for Raspberry Pi. Designed to analyze live data and detect hidden issues like lazy O2 sensors and failing catalytic converters before you buy a used truck.
          </p>
        </div>
      </header>

      <main className="max-w-5xl mx-auto py-12 px-6 grid grid-cols-1 lg:grid-cols-3 gap-8">
        
        {/* Left Column: Project Structure & Info */}
        <div className="lg:col-span-1 space-y-8">
          <section className="bg-white p-6 rounded-xl shadow-sm border border-gray-200">
            <div className="flex items-center gap-2 mb-4 text-slate-800">
              <FolderTree className="w-5 h-5" />
              <h2 className="text-xl font-semibold">Project Structure</h2>
            </div>
            <div className="font-mono text-sm text-gray-600 bg-slate-50 p-4 rounded-lg border border-slate-100">
              <ul className="space-y-2">
                <li className="flex items-center gap-2">
                  <span className="text-blue-500">📁</span> obd-inspection-agent/
                </li>
                <li className="flex items-center gap-2 ml-4">
                  <span className="text-gray-400">📄</span> main.py
                </li>
                <li className="flex items-center gap-2 ml-4">
                  <span className="text-gray-400">📄</span> requirements.txt
                </li>
                <li className="flex items-center gap-2 ml-4">
                  <span className="text-gray-400">📄</span> README.md
                </li>
                <li className="flex items-center gap-2 ml-4 text-gray-400 italic">
                  <span className="text-gray-400">📄</span> obd_inspection_log_*.csv (Generated)
                </li>
              </ul>
            </div>
          </section>

          <section className="bg-blue-50 p-6 rounded-xl border border-blue-100">
            <div className="flex items-center gap-2 mb-3 text-blue-800">
              <Info className="w-5 h-5" />
              <h2 className="text-lg font-semibold">The Mechanic's Logic</h2>
            </div>
            <div className="space-y-4 text-sm text-blue-900 leading-relaxed">
              <div>
                <strong className="block mb-1">The "Lazy" Sensor (Upstream O2):</strong>
                Good: Zigzags fast between 0.1V and 0.9V.<br/>
                Bad: Slow rolling hill or flat. Ruins fuel economy.
              </div>
              <div>
                <strong className="block mb-1">The "Copycat" (Downstream O2):</strong>
                Good: Steady flat line (0.45V–0.6V).<br/>
                Bad: Zigzags like the upstream. Catalytic converter is dead ($1000+ repair).
              </div>
              <div>
                <strong className="block mb-1">Catalyst Temperature:</strong>
                Normal: 400°C to 800°C.<br/>
                Bad: Spikes over 850°C during cruise (clogged/overheating).
              </div>
            </div>
          </section>
        </div>

        {/* Right Column: Code Blocks */}
        <div className="lg:col-span-2 space-y-8">
          <section>
            <div className="flex items-center gap-2 mb-4 text-slate-800">
              <FileCode className="w-5 h-5" />
              <h2 className="text-xl font-semibold">main.py</h2>
            </div>
            <CodeBlock code={pythonCode} language="python" filename="main.py" />
          </section>

          <section>
            <div className="flex items-center gap-2 mb-4 text-slate-800">
              <FileCode className="w-5 h-5" />
              <h2 className="text-xl font-semibold">requirements.txt</h2>
            </div>
            <CodeBlock code={requirementsCode} language="text" filename="requirements.txt" />
          </section>

          <section>
            <div className="flex items-center gap-2 mb-4 text-slate-800">
              <FileCode className="w-5 h-5" />
              <h2 className="text-xl font-semibold">README.md</h2>
            </div>
            <CodeBlock code={readmeCode} language="markdown" filename="README.md" />
          </section>
        </div>

      </main>
    </div>
  );
}
