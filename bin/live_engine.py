"""
live_engine.py — Unified live execution dispatcher (ORB / IB / VWAP).

Reads today's watchlist, checks the strategy field written by the regime
detector, and dispatches each ticker to the matching strategy module's
run() function:

  "ORB"     — lib/strategy_orb.py   5-min Opening Range Breakout,   starts 15:35 NL / 09:35 ET
  "IB"      — lib/strategy_ib.py    60-min Initial Balance Breakout, starts 16:30 NL / 10:30 ET
  "VWAP"    — lib/strategy_vwap.py  VWAP Reversion,                 starts 15:30 NL / 09:30 ET
  "SIT_OUT" — stand aside, no trades today

This file owns ONLY the dispatch loop: load watchlist, pick strategy,
connect to IBKR, spawn one thread per ticker calling that strategy's run(),
join, EOD-flatten, disconnect. It contains no strategy logic and never
calls IBKR directly — all IBKR access goes through ibkr_connector.py via
each strategy module.

Adding a new strategy = drop lib/strategy_xxx.py with a
  run(pick, ib, state, state_lock, profile=None, account_summary=None, **kwargs)
function, then register it in _DISPATCH below. No changes needed here
beyond that one line.

Run via Task Scheduler at ~15:25 NL:
  python bin/live_engine.py
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402

import json
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
import ibkr_connector
import strategy_ib
import strategy_orb
import strategy_vwap
from profiles import get_profile
from session_runtime import (
    _SIGNAL_TO_DIRECTION, _eod_safety_flatten, _load_state, _load_watchlist, _ts,
)
from telegram_notify import send_message

NL = ZoneInfo("Europe/Amsterdam")

_DISPATCH = {
    "ORB":  strategy_orb.run,
    "IB":   strategy_ib.run,
    "VWAP": strategy_vwap.run,
}


def main() -> None:
    profile = get_profile(config.PROFILE)

    print(f"\n{'='*60}")
    print(f"  Signal Mesh Day — Live Engine")
    print(f"  {datetime.now(NL).strftime('%Y-%m-%d %H:%M')} NL")
    print(f"  LIVE_TRADING={config.LIVE_TRADING}  PROFILE={config.PROFILE}")
    print(f"{'='*60}\n")

    # Load watchlist and read strategy chosen by regime detection
    picks = _load_watchlist()

    # Read strategy from watchlist (defaults to ORB for backward compat)
    today_path = Path(config.WATCHLIST_DIR) / f"watchlist_{datetime.now(NL).strftime('%Y%m%d')}.json"
    strategy   = "ORB"
    if today_path.exists():
        with open(today_path) as f:
            strategy = json.load(f).get("strategy", "ORB").upper()

    if strategy == "SIT_OUT":
        if not profile.get("allow_sit_out", True):
            strategy = "VWAP"
            send_message(f"⚠️ Regime says SIT_OUT but profile={config.PROFILE} forces VWAP.")
        else:
            send_message("😴 Regime says SIT_OUT — no trades today.")
            return

    runner = _DISPATCH.get(strategy, strategy_orb.run)
    print(f"  [{_ts()}] Strategy: {strategy}  runner: {runner.__module__}.run")

    actionable = [
        p for p in picks
        if _SIGNAL_TO_DIRECTION.get(p.get("signal", "HOLD"), "skip") != "skip"
    ]
    if not actionable:
        if not profile.get("force_trade"):
            send_message("😴 No actionable picks in today's watchlist (all HOLD).")
            return
        send_message(f"⚠️ No actionable picks — profile={config.PROFILE} will force-fill at deadline.")
        # Re-include all picks; force_fill in the runner handles the rest
        actionable = picks

    send_message(
        f"🚀 <b>Live Engine — {strategy}</b>  ·  profile={config.PROFILE}  ·  {len(actionable)} pick(s)\n"
        + "\n".join(
            f"  {'📈' if p.get('signal')=='BUY' else '📉'} {p['ticker']}  "
            f"{_SIGNAL_TO_DIRECTION.get(p.get('signal','HOLD'),'skip').upper()}"
            for p in actionable
        )
    )

    try:
        ib = ibkr_connector.connect()
    except Exception as e:
        msg = f"🔴 IBKR connection failed: {e}"
        print(msg); send_message(msg)
        return

    # Fetch live account data once — passed into every runner thread for sizing
    account_summary: dict | None = None
    try:
        account_summary = ibkr_connector.get_account_summary(ib)
        print(f"  Account  NLV ${account_summary['net_liquidation']:,.0f}  "
              f"available ${account_summary['available_funds']:,.0f}")
    except Exception as e:
        msg = f"⚠️ Cannot read account summary: {e}. Sizing falls back to config.HOUSE_MONEY_EUR."
        print(msg); send_message(msg)

    state      = _load_state()
    state_lock = threading.Lock()
    max_picks  = profile.get("max_picks", config.MAX_CONCURRENT_POSITIONS)

    threads: list[threading.Thread] = []
    for i, pick in enumerate(actionable[:max_picks]):
        # IB's fetch is a single bulk 60-min-range request per ticker, so it
        # staggers to avoid IBKR pacing throttle; the others poll one bar at
        # a time and don't need it.
        extra = {"stagger_index": i} if strategy == "IB" else {}
        t = threading.Thread(
            target=runner,
            args=(pick, ib, state, state_lock),
            kwargs={"profile": profile, "account_summary": account_summary, **extra},
            name=f"{strategy}-{pick['ticker']}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        print(f"  [{_ts()}] [{pick['ticker']}] {strategy} thread started")

    for t in threads:
        t.join()

    print("\n  Running EOD safety flatten check...")
    _eod_safety_flatten(ib, actionable)

    ib.disconnect()
    send_message(f"✅ {strategy} session complete — all positions flat.")
    print("\nDisconnected cleanly.")


if __name__ == "__main__":
    main()
