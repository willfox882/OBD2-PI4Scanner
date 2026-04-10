@echo off
echo ==========================================
echo   OBD2 UI Sandbox Tester (Windows)
echo ==========================================
echo.
echo Installing required Windows UI packages...
pip install windows-curses pyserial pyyaml pandas matplotlib
echo.
echo Launching the UI... 
echo (Note: The status bar will say DISCONNECTED because there is no truck. This is normal!)
echo.
python -m src.main --port COM99
pause
