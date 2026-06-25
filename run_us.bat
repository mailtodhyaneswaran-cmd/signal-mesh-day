@echo off
cd /d "%~dp0"
echo [%date% %time%] Starting Signal Mesh Day - ORB Engine >> run_us.log
.venv\Scripts\python.exe orb_strategy.py >> run_us.log 2>&1
echo [%date% %time%] ORB Engine finished >> run_us.log
