@echo off
set PYTHONUTF8=1
cd /d "c:\Users\mailt\Desktop\claude_code_course\signal-mesh-day"
echo [%date% %time%] Starting Signal Mesh Day - Premarket Screener >> run_screener.log
"C:\Users\mailt\AppData\Local\Python\pythoncore-3.14-64\python.exe" bin\day_orchestrator.py >> run_screener.log 2>&1
echo [%date% %time%] Screener finished >> run_screener.log
