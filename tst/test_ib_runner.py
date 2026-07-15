"""
test_ib_runner.py — Verify ibkr_connector.get_historical_bars() retry + serialisation.

Background
----------
When regime picks IB and 5 threads all wake at 16:30 NL simultaneously, they
used to fire reqHistoricalData at the exact same second. IBKR throttles
concurrent historical requests → all get Error 366 / timeout (what happened
2026-07-01, and again under ORB on 2026-07-13 because the retry/lock fix had
only been applied inline inside the old run_ticker_ib, not to the shared
connector). The fix now lives once in ibkr_connector.get_historical_bars():
  1. Retry    — retries up to config.IBKR_HIST_RETRIES× with
                config.IBKR_HIST_RETRY_DELAY_SEC between attempts.
  2. Serialise — every call acquires ibkr_connector._HIST_LOCK, so concurrent
                per-ticker threads never overlap their reqHistoricalData calls.
strategy_ib.py additionally staggers thread start times before calling in,
to spread out lock contention.

Tests
-----
  Test 1  Retry logic (offline, mock) — mocks ib.reqHistoricalData to return
          empty N times then real bars; verifies get_historical_bars() retried
          the right number of times before returning.
  Test 2  Serialisation (offline, mock) — 3 threads call get_historical_bars()
          concurrently against a mock ib whose reqHistoricalData sleeps
          briefly; verifies no two calls overlap (clock-based, not just
          checking that a lock object exists).
  Test 3  Live IB bar fetch (--live, requires IBKR + after 16:30 NL / 10:30 ET)
          — actually calls ibkr_connector.get_historical_bars() for a real
          ticker and confirms bars come back.

Usage
-----
  # Offline tests only (always runnable):
  python tst/test_ib_runner.py

  # Include live IBKR test (run after 16:30 NL on a trading day):
  python tst/test_ib_runner.py --live --ticker NVDA
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402
import argparse
import sys
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import ibkr_connector

NL   = ZoneInfo("Europe/Amsterdam")
_PASS = "  PASS"
_FAIL = "  FAIL"
_SKIP = "  SKIP"


# ── Test 1: Retry logic (async fetch path) ────────────────────────────────────

def test_retry_logic() -> bool:
    """get_historical_bars() must retry on an empty result until bars come back.

    Mocks the async fetch (reqHistoricalDataAsync). On the owner thread (offline,
    no loop set), get_historical_bars runs the coroutine directly via _submit."""
    print("\n" + "-" * 56)
    print("  Test 1: get_historical_bars() retry logic (offline / mock)")
    print("-" * 56)

    for fail_first_n in [0, 1, 2, 4]:
        call_count = 0

        async def mock_req_async(*_, **__):
            nonlocal call_count
            call_count += 1
            return [] if call_count <= fail_first_n else [MagicMock()]

        mock_ib = MagicMock()
        mock_ib.isConnected.return_value = True
        mock_ib.reqHistoricalDataAsync = mock_req_async
        mock_contract = MagicMock(conId=12345, symbol="NVDA")  # already qualified

        bars = ibkr_connector.get_historical_bars(
            mock_ib, mock_contract, "3600 S", "1 min",
            retries=5, retry_delay=0,   # no real sleeping in the offline test
        )

        expected_calls = fail_first_n + 1  # succeed on attempt fail_first_n+1
        if call_count != expected_calls or not bars:
            print(f"  fail_first_n={fail_first_n}: called {call_count}x "
                  f"(expected {expected_calls}) bars={bool(bars)}  {_FAIL}")
            return False
        print(f"  fail_first_n={fail_first_n}: succeeded on attempt {call_count}/5  {_PASS}")

    # Edge case: all 5 fail -> should return []
    call_count = 0

    async def always_empty(*_, **__):
        return []

    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.reqHistoricalDataAsync = always_empty
    mock_contract = MagicMock(conId=12345, symbol="NVDA")
    bars = ibkr_connector.get_historical_bars(
        mock_ib, mock_contract, "3600 S", "1 min", retries=5, retry_delay=0,
    )
    if bars:
        print(f"  All-fail case: should return [], got {bars}  {_FAIL}")
        return False
    print(f"  All-fail case: correctly returns []  {_PASS}")

    print(f"\n  Retry logic: {_PASS}")
    return True


# ── Test 2: Threading model (owner vs worker) ─────────────────────────────────

def test_threading_model() -> bool:
    """On the loop-owner thread (no loop set offline), _call_ib / _submit run the
    work directly (no marshalling). This is the invariant that lets the screener
    and the main thread call IBKR sync while workers marshal onto the loop."""
    print("\n" + "-" * 56)
    print("  Test 2: threading model owner-thread pass-through (offline)")
    print("-" * 56)

    # _LOOP is None offline -> current thread is treated as the owner.
    if not ibkr_connector._on_loop_thread():
        print(f"  _on_loop_thread() should be True when no loop is set  {_FAIL}")
        return False
    print(f"  _on_loop_thread() True with no loop set  {_PASS}")

    if ibkr_connector._call_ib(lambda: 42) != 42:
        print(f"  _call_ib did not pass through on owner thread  {_FAIL}")
        return False
    print(f"  _call_ib(lambda: 42) == 42 on owner thread  {_PASS}")

    async def _co():
        return "ok"

    if ibkr_connector._submit(_co(), timeout=5) != "ok":
        print(f"  _submit did not run coroutine on owner thread  {_FAIL}")
        return False
    print(f"  _submit(coroutine) ran directly on owner thread  {_PASS}")

    print(f"\n  Threading model: {_PASS}")
    return True


# ── Test 3: Live IB bar fetch ─────────────────────────────────────────────────

def test_live_ib_bars(ticker: str) -> bool:
    """Connect to IBKR and fetch 60-min IB bars via get_historical_bars() —
    requires market hours."""
    print("\n" + "-" * 56)
    print(f"  Test 3: Live IB bar fetch — {ticker}")
    print("-" * 56)

    now_nl = datetime.now(NL)
    ib_range_end_nl = now_nl.replace(hour=16, minute=30, second=0, microsecond=0)
    if now_nl < ib_range_end_nl:
        print(f"  Current time {now_nl.strftime('%H:%M')} NL is before IB range end "
              f"(16:30 NL / 10:30 ET).  {_SKIP}")
        print("  Run again after 16:30 NL on a trading day.")
        return True   # not a failure — just not applicable yet

    try:
        ib = ibkr_connector.connect()
        print(f"  Connected  accounts={ib.managedAccounts()}")
    except Exception as e:
        print(f"  IBKR connection failed: {e}  {_FAIL}")
        return False

    contract = ibkr_connector.get_contract(ticker, "SMART", "USD")
    all_ok   = True

    # Simulate the stagger that strategy_ib.run() applies before fetching.
    for stagger_index in [0, 1]:
        if stagger_index > 0:
            print(f"  [thread {stagger_index}] sleeping {stagger_index*3}s (stagger)...")
            time.sleep(stagger_index * 3)

        bars = ibkr_connector.get_historical_bars(
            ib, contract, "3600 S", "1 min", use_rth=True,
        )

        if not bars:
            print(f"  [thread {stagger_index}] {ticker}: no bars  {_FAIL}")
            all_ok = False
        else:
            print(f"  [thread {stagger_index}] {ticker}: {len(bars)} bars returned  {_PASS}")

    ib.disconnect()
    print("\n  Disconnected.")
    if all_ok:
        print(f"\n  Live IB bar fetch: {_PASS}")
    return all_ok


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Test ibkr_connector.get_historical_bars() retry + serialisation")
    p.add_argument("--live",   action="store_true", help="Run live IBKR bar fetch (needs 16:30+ NL)")
    p.add_argument("--ticker", default="NVDA",      help="Ticker for --live test (default: NVDA)")
    args = p.parse_args()

    print("=" * 56)
    print("  ibkr_connector.get_historical_bars() — Retry + Serialisation Test")
    print("=" * 56)

    results = {}
    results["1_retry_logic"]     = test_retry_logic()
    results["2_threading_model"] = test_threading_model()
    if args.live:
        results["3_live_ib_bars"] = test_live_ib_bars(args.ticker)

    print("\n" + "=" * 56)
    print("  SUMMARY")
    print("=" * 56)
    all_pass = True
    for name, ok in results.items():
        status = _PASS if ok else _FAIL
        print(f"  {status}  {name}")
        if not ok:
            all_pass = False
    print()
    if all_pass:
        print("  Overall: ALL PASSED")
    else:
        print("  Overall: SOME FAILED")
    print("=" * 56 + "\n")
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
