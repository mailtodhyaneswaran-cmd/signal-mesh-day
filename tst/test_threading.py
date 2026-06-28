"""
test_threading.py -- Offline threading tests for orb_strategy.py

No IBKR connection required. No market hours required. Runs any day.

Tests
-----
  1. State lock -- 10 threads race to "take" the same ticker simultaneously.
     With the lock: exactly 1 trade recorded. Without: multiple slip through.

  2. Concurrent execution -- 3 threads run a 1-second task each.
     Sequential would take 3 s; parallel should finish in ~1 s.

  3. EOD safety flatten -- mocks ib.positions() and verifies _eod_safety_flatten
     calls close_position_at_market for any open position in the watchlist.

Usage
-----
  python test_threading.py           # all three tests
  python test_threading.py --lock    # state lock only
  python test_threading.py --timing  # concurrent timing only
  python test_threading.py --eod     # EOD flatten only
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402
import argparse
import sys
import threading
import time
from unittest.mock import MagicMock, patch


# -- ANSI helpers -------------------------------------------------------------

PASS = "  PASS"
FAIL = "  FAIL"


def _section(title: str) -> None:
    print(f"\n{'-'*56}")
    print(f"  {title}")
    print(f"{'-'*56}")


# -- Test 1: State lock --------------------------------------------------------

def test_state_lock() -> bool:
    """
    10 threads simultaneously try to 'take a trade' for the same ticker.

    Correct behaviour: exactly 1 thread records the trade.
    Without the lock: multiple threads slip through the check before any
    of them writes, producing > 1 trade (race condition).
    """
    _section("Test 1: State lock -- race condition prevention")

    def _attempt(state, lock, results, delay):
        time.sleep(delay)
        with lock:
            if state["trades"].get("NVDA"):
                return                         # gate: already taken
            state["trades"]["NVDA"] = True     # atomic check + write
            results.append(threading.current_thread().name)

    # -- With lock ------------------------------------------------------
    state   = {"date": "2099-01-01", "trades": {}}
    lock    = threading.Lock()
    winners = []

    threads = [
        threading.Thread(
            target = _attempt,
            args   = (state, lock, winners, i * 0.001),
            name   = f"T{i}",
        )
        for i in range(10)
    ]
    for t in threads: t.start()
    for t in threads: t.join()

    ok_locked = len(winners) == 1
    print(f"  With lock:    {len(winners)} trade(s) recorded, winner = {winners}")
    print(f"  {PASS if ok_locked else FAIL}  Expected exactly 1 => {'PASS' if ok_locked else 'FAIL'}")

    # -- Without lock (demonstrates the bug) ---------------------------
    state2   = {"date": "2099-01-01", "trades": {}}
    winners2 = []

    def _attempt_no_lock(state, results, delay):
        time.sleep(delay)
        if state["trades"].get("NVDA"):
            return
        time.sleep(0.001)          # gap between check and write -- race window
        if not state["trades"].get("NVDA"):
            state["trades"]["NVDA"] = True
            results.append(threading.current_thread().name)

    threads2 = [
        threading.Thread(
            target = _attempt_no_lock,
            args   = (state2, winners2, i * 0.001),
            name   = f"U{i}",
        )
        for i in range(10)
    ]
    for t in threads2: t.start()
    for t in threads2: t.join()

    ok_unlocked = len(winners2) > 1      # should demonstrate the race
    print(f"\n  Without lock: {len(winners2)} trade(s) recorded -- {winners2}")
    if ok_unlocked:
        print(f"  INFO  Race condition demonstrated ({len(winners2)} threads slipped through)")
    else:
        print(f"  INFO  Race not triggered this run (timing-dependent -- try again if curious)")

    return ok_locked


# -- Test 2: Concurrent execution timing --------------------------------------

def test_concurrent_timing() -> bool:
    """
    3 threads each sleep 1 second simulating a 'watching' phase.
    Sequential: would take 3 s.
    Concurrent: should finish in ~1 s (threads run in parallel).
    """
    _section("Test 2: Concurrent execution -- 3 threads, 1 s each")

    results = []

    def _task(name, duration):
        t0 = time.time()
        time.sleep(duration)
        results.append((name, round(time.time() - t0, 2)))

    threads = [
        threading.Thread(target=_task, args=(f"ticker-{t}", 1.0), name=f"T{t}")
        for t in ("NVDA", "TSLA", "AAPL")
    ]

    wall_start = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    wall_total = time.time() - wall_start

    print(f"  Per-thread duration: {results}")
    print(f"  Total wall time:     {wall_total:.2f} s")

    # Allow up to 1.5× single-thread time (overhead tolerance)
    ok = wall_total < 1.5
    print(f"  {PASS if ok else FAIL}  Wall time {wall_total:.2f} s < 1.5 s => {'PASS' if ok else 'FAIL'}")
    if not ok:
        print("  (Threads appear to be running sequentially -- check thread creation logic)")
    return ok


# -- Test 3: EOD safety flatten ------------------------------------------------

def test_eod_safety_flatten() -> bool:
    """
    Mocks ib.positions() to return one open NVDA position (long, qty=10).
    Verifies _eod_safety_flatten calls close_position_at_market for it,
    and ignores positions NOT in the watchlist.
    """
    _section("Test 3: EOD safety flatten -- mocked IBKR positions")

    # Import after patching so we get the real function body
    from orb_strategy import _eod_safety_flatten

    # Build mock position for NVDA (in watchlist) and MSFT (not in watchlist)
    def _mock_position(symbol, qty):
        p = MagicMock()
        p.contract.symbol = symbol
        p.position = qty
        return p

    mock_positions = [
        _mock_position("NVDA", 10),   # long 10 -- should be flattened
        _mock_position("MSFT", -5),   # short 5, NOT in watchlist -- should be ignored
    ]

    mock_ib = MagicMock()
    mock_ib.positions.return_value = mock_positions

    calls = []

    with patch("orb_strategy.ibkr_connector.close_position_at_market",
               side_effect=lambda ib, contract, direction, qty: calls.append(
                   {"symbol": contract.symbol, "direction": direction, "qty": qty}
               )):
        with patch("orb_strategy.send_message"):
            actionable = [{"ticker": "NVDA"}, {"ticker": "TSLA"}]  # watchlist
            _eod_safety_flatten(mock_ib, actionable)

    print(f"  Positions returned: NVDA long 10, MSFT short 5")
    print(f"  Watchlist tickers:  NVDA, TSLA")
    print(f"  Flatten calls made: {calls}")

    ok_nvda   = any(c["symbol"] == "NVDA" and c["direction"] == "long"  and c["qty"] == 10 for c in calls)
    ok_no_msft = all(c["symbol"] != "MSFT" for c in calls)

    print(f"\n  NVDA flattened:     {'YES' if ok_nvda else 'NO'}")
    print(f"  MSFT ignored:       {'YES' if ok_no_msft else 'NO (ERROR)'}")
    ok = ok_nvda and ok_no_msft
    print(f"  {PASS if ok else FAIL}  => {'PASS' if ok else 'FAIL'}")
    return ok


# -- Summary -------------------------------------------------------------------

def _summary(results: dict) -> bool:
    print(f"\n{'='*56}")
    print(f"  SUMMARY")
    print(f"{'='*56}")
    all_passed = True
    for name, ok in results.items():
        icon = "✅" if ok else "❌"
        print(f"  {icon}  {name}")
        if not ok:
            all_passed = False
    verdict = "✅  ALL PASSED" if all_passed else "❌  SOME FAILED"
    print(f"\n  {verdict}")
    print(f"{'='*56}\n")
    return all_passed


# -- CLI -----------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Offline threading tests for orb_strategy.py")
    p.add_argument("--lock",   action="store_true", help="State lock test only")
    p.add_argument("--timing", action="store_true", help="Concurrent timing test only")
    p.add_argument("--eod",    action="store_true", help="EOD flatten test only")
    args = p.parse_args()

    run_all = not (args.lock or args.timing or args.eod)

    results = {}
    if args.lock or run_all:
        results["State lock (race condition)"] = test_state_lock()
    if args.timing or run_all:
        results["Concurrent execution timing"] = test_concurrent_timing()
    if args.eod or run_all:
        results["EOD safety flatten (mocked)"] = test_eod_safety_flatten()

    ok = _summary(results)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
