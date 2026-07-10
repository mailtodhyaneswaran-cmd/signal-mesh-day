@echo off
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1
cd /d "%~dp0.."
echo [%date% %time%] Starting Signal Mesh Day - Live Engine >> run_us.log
"C:\Users\mailt\AppData\Local\Python\pythoncore-3.14-64\python.exe" -u bin\live_engine.py >> run_us.log 2>&1
echo [%date% %time%] Live Engine finished >> run_us.log
