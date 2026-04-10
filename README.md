# OBD2 Diagnostic & Control Platform

A comprehensive Python-based OBD2 diagnostic and control platform for Raspberry Pi, designed for in-depth pre-purchase inspections and advanced diagnostics.

## Features
- Real-time live data monitoring with asynchronous polling
- DTC reading and clearing across multiple modules
- Bidirectional controls with safety interlocks
- Capability detection for vehicle modules and PIDs
- Offline analysis of logged session data
- Terminal-based UI using curses

---

## 🛠️ Physical Setup (For Mechanics)

To use this tool on a vehicle, you need the following hardware:
1. **Raspberry Pi 4** (with Raspberry Pi OS installed)
2. **OBDLink EX USB adapter**
3. **Windows Laptop** (for viewing the screen)
4. **Power Source** (e.g., a 12V cigarette-lighter-to-USB-C adapter to power the Pi)

### Step-by-Step Connection:
1. **Plug into the Truck:** Plug the OBDLink EX cable into the truck's OBD2 port (usually under the driver's side dashboard).
2. **Plug into the Pi:** Plug the USB end of the OBDLink EX cable into any USB port on the Raspberry Pi.
3. **Power the Pi:** Plug the USB-C power cable into the Raspberry Pi to turn it on. Wait about 60 seconds for it to fully boot up.
4. **Connect your Laptop:** Connect your Windows laptop to the same Wi-Fi network as the Raspberry Pi (or connect them directly with an Ethernet cable).

---

## 💻 How to Open the Tool (Windows or iPad)

You do not need a separate monitor or mouse for the Raspberry Pi. You will control it directly from your Windows laptop or iPad using a process called "SSH".

### Option A: Using an iPad (Highly Recommended for Vehicles)
Using an iPad is perfect for sitting in the driver's seat. To do this, the Raspberry Pi needs to broadcast its own Wi-Fi network.

**1. Setup the Pi's Wi-Fi Hotspot (One-time setup):**
While the Pi is connected to your home internet, run the included setup script to make it broadcast a Wi-Fi network:
```bash
sudo bash scripts/setup_hotspot.sh
```
**2. Connect the iPad:**
- Take the Pi and OBD cable to the truck and plug them in.
- On your iPad, go to Settings -> Wi-Fi and connect to the network named **OBD_Scanner** (Password: `mechanic123`).
**3. Save the Connection in Termius (One-time setup):**
- Download a free SSH app from the App Store, such as **Termius**.
- Open Termius, go to "Hosts", and tap the **+** button to create a New Host.
- **Alias:** OBD Scanner
- **Hostname or IP Address:** `10.42.0.1`
- **Username:** `pi`
- **Password:** Your Pi's password (default is `raspberry`)
- Tap **Save**. Termius will remember this forever.

**4. The Daily Workflow (In the Shop):**
1. Plug the Pi and OBD cable into the truck.
2. Your iPad will automatically connect to the **OBD_Scanner** Wi-Fi (make sure Auto-Join is on in iPad settings).
3. Open Termius and tap your saved **OBD Scanner** icon. 
4. A black screen with a blinking cursor will appear. **This black screen IS the Raspberry Pi's command terminal.** You are now remotely inside the Pi!
5. To make launching the app incredibly fast, run this setup script once: `bash scripts/setup_alias.sh`. 
6. Now, simply type the letters `obd` using your iPad keyboard and press the **Enter/Return** key. The scanner UI will instantly pop up.

*(Note: Termius provides on-screen arrow keys and an Enter button, making it very easy to navigate the menus!)*

### Option B: Using a Windows Laptop
1. **Open Windows Terminal or Command Prompt:**
   - Click the Windows Start button.
   - Type `cmd` or `Terminal` and press Enter.
2. **Connect to the Pi:**
   - In the black terminal window, type the following command and press Enter:
     ```bash
     ssh pi@raspberrypi.local
     ```
     *(Note: If `raspberrypi.local` doesn't work, you will need to type the Pi's actual IP address)*
3. **Enter Password:**
   - It will ask for a password. Type your Raspberry Pi password (the default is usually `raspberry`) and press Enter. **Note: As you type the password, nothing will show up on the screen. This is normal security. Just type it and press Enter.**
4. **Run the Scanner Tool:**
   - Once logged in, navigate to the folder where this code is saved (e.g., `cd obd2-diag`).
   - Run the tool by typing this command and pressing Enter:
     ```bash
     python -m src.main --port /dev/ttyUSB0
     ```

The interactive scanner menu will immediately appear on your screen!

---

## 📊 Data Logging & Reports

This tool does two things at once:
1. **Live UI:** Shows you live data and lets you run commands.
2. **Background Logger:** The moment you open the tool, it automatically starts recording all data to a CSV file in the `./logs/` folder. You will see a `[REC]` indicator at the bottom of the screen.

When you are done, you can copy these CSV files to your Windows laptop and use them to generate charts and reports of the vehicle's health.

---

## ⚙️ Supported Bidirectional Actuator Tests

The platform includes a full suite of safe, open-source, non-security-locked bidirectional actuator tests for Ford and GM vehicles. 

### Ford Supported Tests:
- EVAP Purge Valve & Vent Solenoid
- Fuel Pump Relay & Fuel Pump Prime
- Cooling Fan Low/High Speed
- Radiator Shutter Command
- Secondary Air Injection Pump & AIR Diverter Valve
- EGR Valve Sweep & Electronic Throttle Control Sweep
- Idle Speed Control Test
- Throttle Body Cleaning Position
- Torque Converter Clutch Apply
- Injector Buzz Test & Cylinder Cut-Out Test
- ABS Pump Motor & Valve Solenoids (Read-only Probes)
- A/C Clutch Command
- HVAC Blend Door Test
- Instrument Cluster Gauge Sweep & Indicator Lamp Test

### General Motors (GM) Supported Tests:
- EVAP Purge Valve, Vent Solenoid, & EVAP Seal Command
- Fuel Pump Relay & Fuel Pump Prime
- Cooling Fan Low/High Speed
- Secondary Air Injection Pump & AIR Diverter Valve
- EGR Valve Sweep & Electronic Throttle Control Sweep
- Idle Speed Control Test
- Torque Converter Clutch Apply
- Injector Balance Test
- ABS Pump Motor & Valve Solenoids (Read-only Probes)
- Steering Assist Pump
- A/C Clutch Command
- HVAC Blend Door Test
- Instrument Cluster Gauge Sweep & Indicator Lamp Test

### ⚠️ Safety Warnings & Compatibility Notes
- **Safety Levels:** All commands are strictly categorized as `[SAFE]`, `[CAUTION]`, or `[DANGER]`. 
- **Interlocks:** Any command marked `[CAUTION]` or `[DANGER]` requires explicit user confirmation before execution.
- **Engine State:** Tests like the Electronic Throttle Control Sweep or Throttle Body Cleaning Position **MUST** be performed with the Engine OFF (Key On, Engine Off - KOEO).
- **Compatibility:** While these are standard UDS/Mode $08 commands, actual support varies by vehicle year, make, model, and ECU firmware. The tool will safely probe the ECU before execution. If a module rejects a command (e.g., NRC 0x31 Request Out of Range), the UI will safely catch the error and display it without crashing.

---

## Installation (For Developers)
1. Clone the repository
2. Install dependencies: `pip install -r requirements.txt`

## Architecture
See the `src/` directory for the modular architecture:
- `core/`: Connection management, configuration, logging, errors
- `obd/`: PIDs, live data, DTCs, bidirectional controls
- `ui/`: Terminal UI and menu system
- `session/`: Session logging and management
- `analysis/`: Offline data analysis
