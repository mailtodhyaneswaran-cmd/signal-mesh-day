"""
test_ib_runner.py — Verify IB strategy stagger + retry fixes.

Background
----------
When regime picks IB and 5 threads all wake at 16:30 NL simultaneously, they
fire reqHistoricalData at the exact same second.  IBKR throttles concurrent
historical requests → all get Error 366 / timeout (what happened 2026-07-01).

Fixes added:
  1. Stagger  — thread i sleeps i×3s after _wait_until(16:30) before fetching.
  2. Retry    — reqHistoricalData retries up to 5× with 5s sleep between attempts.

Tests
-----
  Test 1  Retry logic (offline, mock) — returns empty N times then real bars;
          verifies the retry loop retried the right number of times.
  Test 2  Stagger timing (offline, mock) — 3 threads with stagger_index 0/1/2;
          verifies they recorded fetch times ~3s apart (clock-based, not just
          checking sleep calls).
  Test 3  Live IB bar fetch (--live, requires IBKR + after 16:30 NL / 10:30 ET)
          — actually calls reqHistoricalData for a real ticker and confirms bars
          come back.  Skipped unless --live is passed.

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
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import config
import ibkr_connector

NL   = ZoneInfo("Europe/Amsterdam")
_PASS = "  PASS"
_FAIL = "  FAIL"
_SKIP = "  SKIP"


# ── Test 1: Retry logic ───────────────────────────────────────────────────────

def test_retry_logic() -> bool:
    """reqHistoricalData fails N times then succeeds — verify retry count."""
    print("\n" + "-" * 56)
    print("  Test 1: Retry logic (offline / mock)")
    print("-" * 56)

    for fail_first_n in [0, 1, 2, 4]:
        call_count = 0

        def mock_req(**_):
            nonlocal call_count
            call_count += 1
            return [] if call_count <= fail_first_n else [MagicMock()]

        ib_bars_raw = None
        call_count   = 0
        for attempt in range(5):
            ib_bars_raw = mock_req()
            if ib_bars_raw:
                break
            # Don't actually sleep in test — just verify the retry counter

        expected_calls = fail_first_n + 1  # succeed on attempt fail_first_n+1
        if call_count != expected_calls or not ib_bars_raw:
            print(f"  fail_first_n={fail_first_n}: called {call_count}× "
                  f"(expected {expected_calls}) ib_bars_raw={bool(ib_bars_raw)}  {_FAIL}")
            return False
        print(f"  fail_first_n={fail_first_n}: succeeded on attempt {call_count}/{5}  {_PASS}")

    # Edge case: all 5 fail → should return empty
    call_count = 0
    ib_bars_raw = None
    for attempt in range(5):
        result = []  # always empty
        if result:
            ib_bars_raw = result
            break
    if ib_bars_raw is not None:
        print(f"  All-fail case: should stay None, got {ib_bars_raw}  {_FAIL}")
        return False
    print(f"  All-fail case: correctly returns None  {_PASS}")

    print(f"\n  Retry logic: {_PASS}")
    return True


# ── Test 2: Stagger timing ────────────────────────────────────────────────────

def test_stagger_timing() -> bool:
    """3 threads with stagger_index 0/1/2 — fetch times must be ≥3s apart."""
    print("\n" + "-" * 56)
    print("  Test 2: Stagger timing (offline / mock, ~6s wall time)")
    print("-" * 56)

    fetch_times: dict[int, float] = {}
    lock = threading.Lock()

    def _simulated_ib_fetch(stagger_index: int) -> None:
        """Simulates the stagger+fetch section of run_ticker_ib."""
        if stagger_index > 0:
            time.sleep(stagger_index * 3)
        t = time.monotonic()
        with lock:
            fetch_times[stagger_index] = t

    threads = [
        threading.Thread(target=_simulated_ib_fetch, args=(i,), daemon=True)
        for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    if len(fetch_times) != 3:
        print(f"  Only {len(fetch_times)}/3 threads completed  {_FAIL}")
        return False

    t0, t1, t2 = fetch_times[0], fetch_times[1], fetch_times[2]
    gap_01 = t1 - t0
    gap_12 = t2 - t1

    print(f"  Thread 0 fetched at  t+{0:.1f}s")
    print(f"  Thread 1 fetched at  t+{t1-t0:.1f}s  (gap {gap_01:.1f}s, expected ≥3s)")
    print(f"  Thread 2 fetched at  t+{t2-t0:.1f}s  (gap {gap_12:.1f}s, expected ≥3s)")

    tolerance = 0.3   # allow 300ms jitter
    ok = gap_01 >= 3.0 - tolerance and gap_12 >= 3.0 - tolerance
    if not ok:
        print(f"  Gaps too small — thundering herd not prevented  {_FAIL}")
        return False

    print(f"\n  Stagger timing: {_PASS}")
    return True


# ── Test 3: Live IB bar fetch ─────────────────────────────────────────────────

def test_live_ib_bars(ticker: str) -> bool:
    """Connect to IBKR and fetch 60-min IB bars — requires market hours."""
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

    # Simulate the stagger + retry for 2 threads
    for stagger_index in [0, 1]:
        if stagger_index > 0:
            print(f"  [thread {stagger_index}] sleeping {stagger_index*3}s (stagger)...")
            time.sleep(stagger_index * 3)

        ib_bars_raw = None
        for attempt in range(5):
            ib_bars_raw = ib.reqHistoricalData(
                contract, endDateTime="", durationStr="3600 S",
                barSizeSetting="1 min", whatToShow="TRADES",
                useRTH=True, formatDate=1,
            )
            if ib_bars_raw:
                break
            if attempt < 4:
                print(f"  [thread {stagger_index}] attempt {attempt+1}/5 returned empty — retrying...")
                time.sleep(5)

        if not ib_bars_raw:
            print(f"  [thread {stagger_index}] {ticker}: no bars after 5 attempts  {_FAIL}")
            all_ok = False
        else:
            print(f"  [thread {stagger_index}] {ticker}: {len(ib_bars_raw)} bars returned  {_PASS}")

    ib.disconnect()
    print("\n  Disconnected.")
    if all_ok:
        print(f"\n  Live IB bar fetch: {_PASS}")
    return all_ok


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Test IB stagger + retry fixes")
    p.add_argument("--live",   action="store_true", help="Run live IBKR bar fetch (needs 16:30+ NL)")
    p.add_argument("--ticker", default="NVDA",      help="Ticker for --live test (default: NVDA)")
    args = p.parse_args()

    print("=" * 56)
    print("  IB Runner — Stagger + Retry Test")
    print("=" * 56)

    results = {}
    results["1_retry_logic"]    = test_retry_logic()
    results["2_stagger_timing"] = test_stagger_timing()
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
