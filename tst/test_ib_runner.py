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


# ── Test 1: Retry logic ───────────────────────────────────────────────────────

def test_retry_logic() -> bool:
    """get_historical_bars() must retry on an empty result until bars come back."""
    print("\n" + "-" * 56)
    print("  Test 1: get_historical_bars() retry logic (offline / mock)")
    print("-" * 56)

    for fail_first_n in [0, 1, 2, 4]:
        call_count = 0

        def mock_req(*_, **__):
            nonlocal call_count
            call_count += 1
            return [] if call_count <= fail_first_n else [MagicMock()]

        mock_ib = MagicMock()
        mock_ib.reqHistoricalData.side_effect = mock_req
        mock_contract = MagicMock(conId=12345, symbol="NVDA")  # already qualified

        bars = ibkr_connector.get_historical_bars(
            mock_ib, mock_contract, "3600 S", "1 min",
            retries=5, retry_delay=0,   # no real sleeping in the offline test
        )

        expected_calls = fail_first_n + 1  # succeed on attempt fail_first_n+1
        if call_count != expected_calls or not bars:
            print(f"  fail_first_n={fail_first_n}: called {call_count}× "
                  f"(expected {expected_calls}) bars={bool(bars)}  {_FAIL}")
            return False
        print(f"  fail_first_n={fail_first_n}: succeeded on attempt {call_count}/5  {_PASS}")

    # Edge case: all 5 fail → should return []
    mock_ib = MagicMock()
    mock_ib.reqHistoricalData.return_value = []
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


# ── Test 2: Serialisation ──────────────────────────────────────────────────────

def test_serialisation() -> bool:
    """3 threads calling get_historical_bars() concurrently must never overlap
    their reqHistoricalData calls — _HIST_LOCK should serialise them."""
    print("\n" + "-" * 56)
    print("  Test 2: get_historical_bars() serialisation (offline / mock)")
    print("-" * 56)

    overlap_detected = threading.Event()
    active = threading.Lock()
    currently_running = {"n": 0}
    guard = threading.Lock()

    def mock_req(*_, **__):
        with guard:
            currently_running["n"] += 1
            if currently_running["n"] > 1:
                overlap_detected.set()
        time.sleep(0.2)   # simulate a slow IBKR round-trip
        with guard:
            currently_running["n"] -= 1
        return [MagicMock()]

    def _fetch(results, idx):
        mock_ib = MagicMock()
        mock_ib.reqHistoricalData.side_effect = mock_req
        mock_contract = MagicMock(conId=12345, symbol=f"T{idx}")
        bars = ibkr_connector.get_historical_bars(
            mock_ib, mock_contract, "3600 S", "1 min", retries=1,
        )
        results[idx] = bool(bars)

    results = {}
    threads = [
        threading.Thread(target=_fetch, args=(results, i), daemon=True)
        for i in range(3)
    ]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    elapsed = time.monotonic() - t0

    if overlap_detected.is_set():
        print(f"  Two or more reqHistoricalData calls ran concurrently  {_FAIL}")
        return False
    if not all(results.values()):
        print(f"  Not all threads got bars: {results}  {_FAIL}")
        return False
    # 3 calls serialised at 0.2s each should take >= ~0.6s wall time
    print(f"  3 calls completed serially in {elapsed:.2f}s (expected >= 0.5s)")
    if elapsed < 0.5:
        print(f"  Calls finished too fast to have been serialised  {_FAIL}")
        return False

    print(f"\n  Serialisation: {_PASS}")
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
    results["1_retry_logic"]    = test_retry_logic()
    results["2_serialisation"]  = test_serialisation()
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
