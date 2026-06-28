@echo off
set PYTHONUTF8=1
cd /d "%~dp0.."
echo [%date% %time%] Starting Signal Mesh Day - ORB Engine >> run_us.log
"C:\Users\mailt\AppData\Local\Python\pythoncore-3.14-64\python.exe" bin\orb_strategy.py >> run_us.log 2>&1
echo [%date% %time%] ORB Engine finished >> run_us.log
