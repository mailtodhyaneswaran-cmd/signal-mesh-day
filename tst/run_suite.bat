@echo off
set PYTHONUTF8=1
cd /d "%~dp0.."
set PY="C:\Users\mailt\AppData\Local\Python\pythoncore-3.14-64\python.exe"

echo.
echo ============================================================
echo  Signal Mesh Day -- Full System Test Suite
echo ============================================================

echo.
echo [1/5] IBKR connection + EUR/USD rate + market data
%PY% tst\test_connection.py
if errorlevel 1 goto :fail

echo.
echo [2/5] yfinance price + news + real RVOL + AI mesh (NVDA)
%PY% tst\test_rvol.py --ticker NVDA --ai
if errorlevel 1 goto :fail

echo.
echo [3/5] Threading tests (offline, no IBKR needed)
%PY% -X utf8 tst\test_threading.py
if errorlevel 1 goto :fail

echo.
echo [4/5] Premarket screener -- AI mesh on NVDA + MU + TSLA
%PY% bin\day_orchestrator.py --tickers NVDA MU TSLA
if errorlevel 1 goto :fail

echo.
echo [5/5] Integration test -- IBKR order placement + verify
%PY% tst\test_integration.py --ticker NVDA --date 2026-06-24 --no-cancel
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo  ALL TESTS PASSED
echo ============================================================
goto :end

:fail
echo.
echo ============================================================
echo  TEST SUITE FAILED (see above)
echo ============================================================
exit /b 1

:end
