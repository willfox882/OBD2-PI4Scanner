# OBD2 Bi-Directional Controls: Operational Guide

This document provides a comprehensive map of all bi-directional (Output Control) commands available in this platform. It explains the practical implementation of each command, why the test is performed, and the diagnostic consequences if a test fails.

---

## 🌬️ EVAP System (Evaporative Emissions)
Tests in this category are used to diagnose fuel vapor leaks and solenoid functionality.

| Command Name | OEM | Practical Action | Why Test? | Consequence of Failure |
| :--- | :--- | :--- | :--- | :--- |
| **EVAP_Purge_Valve** | Ford, GM, RAM | Pulses the purge solenoid to allow vacuum into the canister. | Verifies the solenoid isn't stuck closed or restricted. | **Stuck Open:** Rough idle/stalling after refueling. **Stuck Closed:** "Check Engine" light (P0443). |
| **EVAP_Vent_Solenoid** | Ford, GM | Commands the vent solenoid to close (normally open). | Required to seal the system for a pressure/vacuum leak test. | **Failed Test:** Large leak detected (P0455). Can prevent the vehicle from passing emissions state inspections. |
| **EVAP_Seal_Command** | GM | Closes both Vent and Purge to fully isolate the tank. | Used for "Smoke Testing" or observing vacuum decay over time. | **Failed Test:** Indication of a cracked charcoal canister, leaking fuel cap, or rotted vapor lines. |

---

## ⛽ Fuel System
Tests designed to verify fuel delivery and individual cylinder health.

| Command Name | OEM | Practical Action | Why Test? | Consequence of Failure |
| :--- | :--- | :--- | :--- | :--- |
| **Fuel_Pump_Relay** | Ford, GM | Manually toggles the fuel pump relay on/off. | Verifies the PCM-to-relay wiring and the relay itself. | **Failure:** No-start condition. If the pump runs via tool but not key, the ignition switch or PCM driver is faulty. |
| **Fuel_Pump_Prime** | Ford, GM | Runs the pump for a short burst (3-5 sec). | Safely builds rail pressure before starting a vehicle that has sat for weeks. | **Failure:** Indicates a dead pump or blown fuse. |
| **Injector_Disable_1** | Ford | Cuts the electrical signal to fuel injector #1. | Used for "Power Balance" testing to see if a misfire is fuel-related. | **Failure:** If RPM doesn't drop when disabled, that cylinder was already dead (misfiring). |
| **Injector_Buzz_Test** | Ford | Rapidly pulses injectors while the engine is OFF. | Audible confirmation that injectors are firing and not electrically open. | **Failure:** Identifies broken wiring harnesses (common on diesel trucks) without disassembly. |
| **Injector_Balance_Test**| GM | Measures the exact pressure drop when an injector is pulsed. | Identifies partially clogged injectors that cause lean conditions. | **Failure:** Poor fuel economy and "Lean" codes (P0171/P0174). |

---

## ❄️ Cooling & Air Management
Tests for intake air, EGR, and engine temperature management.

| Command Name | OEM | Practical Action | Why Test? | Consequence of Failure |
| :--- | :--- | :--- | :--- | :--- |
| **Cooling_Fan (L/H)** | Ford, GM | Forces cooling fans to run at Low or High speeds. | Verifies fan motors and high-current relays. | **Failure:** Engine overheating in traffic or A/C blowing warm while stopped. |
| **EGR_Valve_Sweep** | Ford, GM | Moves the EGR valve from 0% to 100%. | Checks for carbon buildup that physically prevents valve movement. | **Failure:** "EGR Flow" codes (P0401). Can cause "pinging" or detonation under load. |
| **Electronic_Throttle_Sweep** | Ford, GM | Opens and closes the throttle plate (Engine OFF). | Checks for "geartrain" failure inside the electronic throttle body. | **Failure:** "Limp Home Mode" (P2135). **DANGER:** High finger-pinch hazard if hands are near the intake. |
| **AIR_Diverter_Valve** | Ford, GM | Redirects secondary air injection to the exhaust. | Verifies the "Smog Pump" system is reducing cold-start emissions. | **Failure:** Premature catalytic converter failure and P0411 codes. |

---

## ⚙️ Drivetrain & Transmission
Advanced tests for transmission solenoids and engine idle stability.

| Command Name | OEM | Practical Action | Why Test? | Consequence of Failure |
| :--- | :--- | :--- | :--- | :--- |
| **TCC_Apply** | Ford, GM | Manually engages the Torque Converter Clutch. | Verifies the transmission can "lock up" at highway speeds. | **Failure:** Transmission overheating and poor highway fuel economy. **DANGER:** Engaging at idle will stall the engine. |
| **Idle_Speed_Control** | Ford, GM | Overrides the PCM's target RPM (e.g., set to 1200). | Tests the PCM's ability to compensate for loads (like A/C engagement). | **Failure:** Engine stalls when coming to a stop or when the steering wheel is turned. |

---

## 🛑 Brakes & Chassis
Verification of safety-critical systems.

| Command Name | OEM | Practical Action | Why Test? | Consequence of Failure |
| :--- | :--- | :--- | :--- | :--- |
| **ABS_Pump_Motor_Probe**| Ford, GM | Briefly pings the ABS pump to check current draw. | Ensures the ABS system can generate hydraulic pressure. | **Failure:** ABS warning light. In an emergency, the wheels will lock up and skid. |
| **ABS_Bleed_Routine** | RAM | Automatically cycles valves while the tech pumps brakes. | Removes trapped air from the ABS module that manual bleeding can't reach. | **Failure:** "Spongy" brake pedal and reduced stopping power. |
| **Steering_Assist_Pump**| GM | Commands the Electric Power Steering (EPS) motor. | Verifies the motor can generate assist torque. | **Failure:** Extremely heavy steering, making the truck nearly impossible to turn at low speeds. |

---

## 🖥️ Interior & Comfort
Tests for dashboard and cabin electronics.

| Command Name | OEM | Practical Action | Why Test? | Consequence of Failure |
| :--- | :--- | :--- | :--- | :--- |
| **Gauge_Sweep** | Ford, GM | Sweeps all needles (Speed, RPM, Temp) to MAX. | Verifies the stepper motors in the instrument cluster are healthy. | **Failure:** Incorrect speed or temp readings despite healthy sensors. |
| **AC_Clutch_Command** | Ford, GM | Engages the A/C compressor electromagnetic clutch. | Verifies the clutch air gap and PCM relay. | **Failure:** No A/C. If the tool engages the clutch but the dashboard button doesn't, the HVAC controller is faulty. |

---

## ⚠️ SAFETY REMINDERS
1. **DANGER Level Tests:** Commands like *Cylinder Cut Out* or *Throttle Sweep* should only be performed with the vehicle in Park and the hood clear of bystanders.
2. **CAUTION Level Tests:** Commands like *Cooling Fan* can start unexpectedly; keep hands and clothing away from moving parts.
3. **Automatic Abort:** The platform will automatically send a `ReturnControlToECU` command whenever you exit a test menu to ensure the vehicle returns to normal operating mode.
