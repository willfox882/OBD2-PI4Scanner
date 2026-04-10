#!/bin/bash
# This script creates a shortcut command so you don't have to type the full python path every time.

# Ensure we are not running as root for this one, we want it in the pi user's bashrc
if [ "$EUID" -eq 0 ]; then
  echo "Please run this script as your normal user, NOT with sudo."
  exit 1
fi

# Add the alias to the end of the .bashrc file
echo "" >> ~/.bashrc
echo "# OBD2 Scanner Shortcut" >> ~/.bashrc
echo 'alias obd="cd ~/obd2-diag && python -m src.main --port /dev/ttyUSB0"' >> ~/.bashrc

echo "========================================="
echo " Shortcut Created!"
echo " "
echo " From now on, when you open Termius, just type:"
echo " obd"
echo " and press Enter to launch the scanner."
echo "========================================="
