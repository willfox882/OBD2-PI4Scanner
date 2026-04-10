#!/bin/bash
# OBD2 Scanner - Raspberry Pi Hotspot Setup Script
# This script configures the Raspberry Pi to broadcast its own Wi-Fi network.
# This allows an iPad or laptop to connect directly to the Pi while inside a vehicle.

# Ensure the script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (use sudo ./setup_hotspot.sh)"
  exit 1
fi

echo "========================================="
echo " Configuring Raspberry Pi Wi-Fi Hotspot  "
echo "========================================="

SSID="OBD_Scanner"
PASSWORD="mechanic123"

# Check if NetworkManager is installed (Default on modern Raspberry Pi OS)
if ! command -v nmcli &> /dev/null; then
    echo "Error: NetworkManager (nmcli) is not installed."
    echo "This script requires a modern Raspberry Pi OS (Bookworm or newer)."
    exit 1
fi

echo "1. Removing any existing hotspot configuration..."
nmcli connection delete OBD_Hotspot &> /dev/null

echo "2. Creating new Wi-Fi Hotspot ($SSID)..."
nmcli connection add type wifi ifname wlan0 con-name OBD_Hotspot autoconnect yes ssid "$SSID"

echo "3. Configuring hotspot settings (Access Point mode)..."
nmcli connection modify OBD_Hotspot 802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared

echo "4. Setting Wi-Fi password..."
nmcli connection modify OBD_Hotspot wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASSWORD"

echo "5. Starting the hotspot..."
nmcli connection up OBD_Hotspot

echo "========================================="
echo " Setup Complete!"
echo " "
echo " Network Name (SSID): $SSID"
echo " Password: $PASSWORD"
echo " "
echo " Your iPad can now connect to this Wi-Fi network."
echo " Once connected, SSH into the Pi using:"
echo " ssh pi@10.42.0.1"
echo "========================================="
