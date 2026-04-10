import os
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
        
        summary_text = f"Log File Analyzed: {os.path.basename(filepath)}\n"
        summary_text += f"Analysis Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        summary_text += f"Total Data Points: {len(df)}\n"
        summary_text += f"Duration: {(df['Timestamp'].max() - df['Timestamp'].min()).total_seconds():.1f} seconds\n\n"
        
        summary_text += "--- SUMMARY STATISTICS ---\n"
        stats = df.describe().loc[['mean', 'min', 'max', 'std']]
        summary_text += stats.to_string() + "\n\n"
        
        summary_text += "--- ANOMALY DETECTION ---\n"
        if not anomalies:
            summary_text += "No critical anomalies detected. Vehicle health appears normal.\n"
        else:
            for a in anomalies:
                summary_text += f"- {a}\n"
                
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
