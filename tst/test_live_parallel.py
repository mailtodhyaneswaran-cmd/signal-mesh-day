"""
test_live_parallel.py — reproduce the live-session thread hang.

The live engine spawns one thread per watchlist ticker; each thread fetches
IBKR data concurrently (exactly what happened on 2026-07-14, where the session
wedged right after the ORB threads started). ib_async drives a single asyncio
event loop, so multiple threads calling into `ib` at once can deadlock.

This harness mimics that: it loads a watchlist, spawns one thread per pick, and
each thread repeatedly calls ibkr_connector.get_historical_bars() (the exact
opening-range / poll fetch path, which auto-qualifies + fetches). A watchdog
thread force-exits if the workers don't all finish in time, printing which
threads are stuck so the hang is visible instead of hanging the terminal.

Usage
-----
  python tst/test_live_parallel.py                       # uses latest watchlist
  python tst/test_live_parallel.py --date 20260714       # specific watchlist
  python tst/test_live_parallel.py --tickers APP SNDK MRVL
  python tst/test_live_parallel.py --iters 3 --timeout 90
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402
import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
import ibkr_connector

NL = ZoneInfo("Europe/Amsterdam")


def _log(msg: str) -> None:
    print(f"  [{datetime.now(NL).strftime('%H:%M:%S')}] {msg}", flush=True)


def _load_tickers(date_str: str | None, override: list[str] | None) -> list[str]:
    if override:
        return [t.upper() for t in override]
    wl_dir = Path(config.WATCHLIST_DIR)
    if date_str:
        path = wl_dir / f"watchlist_{date_str}.json"
    else:
        files = sorted(wl_dir.glob("watchlist_*.json"))
        if not files:
            print("No watchlist files found."); sys.exit(1)
        path = files[-1]
    doc = json.loads(path.read_text())
    print(f"  Watchlist: {path.name}  strategy={doc.get('strategy')}")
    return [p["ticker"] for p in doc.get("picks", [])]


def _worker(ib, ticker: str, iters: int, status: dict) -> None:
    """Mimic a live ticker thread: fetch bars repeatedly, concurrently."""
    contract = ibkr_connector.get_contract(ticker, "SMART", "USD")
    for i in range(iters):
        status[ticker] = f"iter {i+1}/{iters}: fetching"
        _log(f"[{ticker}] fetch {i+1}/{iters} start")
        t0 = time.monotonic()
        bars = ibkr_connector.get_historical_bars(
            ib, contract, "1800 S", "1 min", use_rth=True, retries=1,
        )
        dt = time.monotonic() - t0
        _log(f"[{ticker}] fetch {i+1}/{iters} done in {dt:.1f}s -> {len(bars)} bars")
    status[ticker] = "DONE"


def _watchdog(status: dict, timeout: float, done_evt: threading.Event) -> None:
    if done_evt.wait(timeout):
        return
    print("\n" + "=" * 60, flush=True)
    print(f"  WATCHDOG: workers did not finish within {timeout:.0f}s — HANG DETECTED", flush=True)
    for tk, st in status.items():
        print(f"    {tk}: {st}", flush=True)
    print("  This is the deadlock. Force-exiting.", flush=True)
    print("=" * 60, flush=True)
    os._exit(2)


def main() -> None:
    p = argparse.ArgumentParser(description="Reproduce the live-session parallel thread hang")
    p.add_argument("--date", help="Watchlist date YYYYMMDD (default: latest file)")
    p.add_argument("--tickers", nargs="+", help="Override tickers instead of a watchlist")
    p.add_argument("--iters", type=int, default=3, help="Fetches per thread (default 3)")
    p.add_argument("--timeout", type=float, default=90, help="Watchdog timeout seconds (default 90)")
    args = p.parse_args()

    tickers = _load_tickers(args.date, args.tickers)
    print(f"\n{'='*60}")
    print(f"  Parallel live-session hang test  —  {len(tickers)} ticker(s)")
    print(f"  Tickers: {', '.join(tickers)}")
    print(f"  {args.iters} concurrent fetches/thread, watchdog {args.timeout:.0f}s")
    print(f"{'='*60}\n")

    ib = ibkr_connector.connect()
    print(f"  Connected  accounts={ib.managedAccounts()}\n", flush=True)

    status:   dict = {t: "pending" for t in tickers}
    done_evt = threading.Event()
    wd = threading.Thread(target=_watchdog, args=(status, args.timeout, done_evt), daemon=True)
    wd.start()

    t0 = time.monotonic()
    threads = [
        threading.Thread(target=_worker, args=(ib, t, args.iters, status), name=t, daemon=True)
        for t in tickers
    ]
    # Same pattern as the live engine: start workers, pump the loop on the main
    # (loop-owner) thread until they finish. Workers marshal their IBKR calls here.
    ibkr_connector.run_threads_pumping(ib, threads)
    done_evt.set()
    elapsed = time.monotonic() - t0

    ib.disconnect()

    print(f"\n{'='*60}")
    all_done = all(v == "DONE" for v in status.values())
    if all_done:
        print(f"  [PASS] ALL {len(tickers)} THREADS COMPLETED in {elapsed:.1f}s -- no deadlock")
    else:
        print(f"  [FAIL] Some threads did not complete: {status}")
    print(f"{'='*60}\n")
    sys.exit(0 if all_done else 1)


if __name__ == "__main__":
    main()
