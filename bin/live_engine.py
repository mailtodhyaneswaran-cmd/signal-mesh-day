"""
live_engine.py — Unified live execution engine (ORB / IB / VWAP).

Reads today's watchlist, checks the strategy field written by the regime
detector, and dispatches each ticker to the correct session runner:

  "ORB"   — 5-min Opening Range Breakout   starts at 15:35 NL / 09:35 ET
  "IB"    — 60-min Initial Balance Breakout starts at 16:30 NL / 10:30 ET
  "VWAP"  — VWAP Reversion                 starts at 15:30 NL / 09:30 ET
  "SIT_OUT" — stand aside, no trades today

All three share the same bracket order / monitoring / EOD flatten logic
from orb_strategy.py.  Only the signal-detection phase differs.

Run via Task Scheduler at ~15:25 NL:
  python bin/live_engine.py
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
import ibkr_connector
from orb_core import (
    ORBConfig, Bar, capture_opening_range,
    detect_breakout, confirm_retest, build_bracket,
)
from telegram_notify import send_message
from vwap_strategy import VWAPConfig, compute_vwap, detect_vwap_setup

# Re-use all shared helpers from orb_strategy (don't duplicate)
from orb_strategy import (
    _SIGNAL_TO_DIRECTION,
    _risk_usd, _today_at, _wait_until,
    _load_state, _save_state,
    _monitor_bracket, _eod_safety_flatten,
    run_ticker_orb,
)

NL             = ZoneInfo("Europe/Amsterdam")
US_OPEN        = "15:30"   # 09:30 ET
US_SESSION_END = "22:00"   # 16:00 ET — hard EOD flatten

# ORB timings
US_ORB_WINDOW_END  = "19:00"  # 13:00 ET — stop looking for ORB breakout

# IB timings
US_IB_RANGE_END   = "16:30"  # 10:30 ET — 60-min IB range closes
US_IB_WINDOW_END  = "17:00"  # 11:00 ET — stop looking for IB breakout

# VWAP timings
US_VWAP_WINDOW_END = "20:00"  # 14:00 ET — stop VWAP reversion


# ── IB session runner ─────────────────────────────────────────────────────────

def run_ticker_ib(pick: dict, ib, state: dict, state_lock: threading.Lock) -> None:
    """60-min Initial Balance Breakout session for one ticker.

    Waits for the first 60 minutes of trading to close (10:30 ET),
    captures the IB range, then watches for a breakout + retest using
    the same orb_core primitives as ORB — just with a wider range.
    """
    ticker      = pick["ticker"]
    signal      = pick.get("signal", "HOLD")
    direction   = _SIGNAL_TO_DIRECTION.get(signal, "skip")
    currency    = pick.get("currency", "USD")
    session_end = _today_at(US_SESSION_END)
    window_end  = _today_at(US_IB_WINDOW_END)

    if direction == "skip":
        send_message(f"😴 {ticker} — HOLD signal, skipping IB today.")
        return

    with state_lock:
        if state["trades"].get(ticker):
            return

    send_message(
        f"🔔 IB watching <b>{ticker}</b>  ·  bias: {direction.upper()}\n"
        f"  Waiting for 60-min IB range to close at {US_IB_RANGE_END} NL..."
    )

    # ── Wait for 60-min IB range to close ────────────────────────────────
    _wait_until(_today_at(US_IB_RANGE_END))

    contract = ibkr_connector.get_contract(ticker, "SMART", currency)

    # Fetch the 60 x 1-min RTH bars that formed the IB range
    ib_bars_raw = ib.reqHistoricalData(
        contract, endDateTime="", durationStr="3600 S",
        barSizeSetting="1 min", whatToShow="TRADES",
        useRTH=True, formatDate=1,
    )
    if not ib_bars_raw:
        msg = f"⚠️ {ticker}: no IB range bars available — skipping."
        print(msg); send_message(msg)
        return

    # Filter to bars from 15:30–16:30 NL (09:30–10:30 ET)
    ib_bars = [
        Bar(
            t=b.date.astimezone(NL).strftime("%H:%M"),
            open=b.open, high=b.high, low=b.low, close=b.close, volume=float(b.volume),
        )
        for b in ib_bars_raw
        if US_OPEN <= b.date.astimezone(NL).strftime("%H:%M") < US_IB_RANGE_END
    ]
    if len(ib_bars) < 10:
        msg = f"⚠️ {ticker}: only {len(ib_bars)} IB bars — skipping."
        print(msg); send_message(msg)
        return

    # Build ORBConfig tuned for IB (60-min range, RVOL disabled)
    cfg = ORBConfig.from_params(config.INTRADAY_PARAMS)
    cfg.range_minutes = len(ib_bars)   # however many bars we actually got
    cfg.rvol_mode     = "disabled"
    cfg.rvol_min      = 0.0

    orng = capture_opening_range(ib_bars, cfg, ticker)
    if orng is None:
        msg = f"😴 {ticker} IB range too thin — skipping."
        print(msg); send_message(msg)
        return

    send_message(
        f"📊 <b>{ticker}</b> IB range: {orng.low:.2f}–{orng.high:.2f} {currency}"
        f"  ·  bias: {direction.upper()}"
    )

    # ── Poll 1-min bars for breakout → retest (same as ORB) ──────────────
    direction_confirmed = None
    polls_per_sec       = config.INTRADAY_PARAMS.poll_interval_sec

    while datetime.now(NL) < window_end:
        bar_raw = ibkr_connector.get_latest_closed_1min_bar(ib, contract)
        if bar_raw is None:
            time.sleep(polls_per_sec)
            continue

        bar = Bar(
            t=bar_raw.date.astimezone(NL).strftime("%H:%M"),
            open=bar_raw.open, high=bar_raw.high,
            low=bar_raw.low, close=bar_raw.close, volume=float(bar_raw.volume),
        )

        if direction_confirmed is None:
            if detect_breakout(orng, bar, direction):
                direction_confirmed = direction
                lvl = orng.high if direction == "long" else orng.low
                send_message(
                    f"{'📈' if direction=='long' else '📉'} <b>{ticker}</b> IB "
                    f"broke {'above' if direction=='long' else 'below'} {lvl:.2f} "
                    f"— watching for retest..."
                )
        else:
            retest = confirm_retest(orng, bar, cfg, direction_confirmed)
            if retest is False:
                send_message(f"⚠️ {ticker} IB failed retest — skipping today.")
                return
            if retest is True:
                entry_price = orng.high if direction_confirmed == "long" else orng.low
                bracket     = build_bracket(orng, entry_price, direction_confirmed, cfg, _risk_usd())
                if bracket["qty"] == 0:
                    send_message(f"⚠️ {ticker} IB qty=0 (stop too wide) — skipping.")
                    return

                action = "BUY" if direction_confirmed == "long" else "SELL"
                trades = ibkr_connector.place_bracket_order(
                    ib, contract, action, bracket["qty"],
                    bracket["entry"], bracket["take_profit"], bracket["stop"],
                )
                send_message(
                    f"✅ IB {'Long' if direction_confirmed=='long' else 'Short'} <b>{ticker}</b>  "
                    f"entry {bracket['entry']:.2f}  SL {bracket['stop']:.2f}  "
                    f"TP {bracket['take_profit']:.2f}  qty {bracket['qty']}"
                )
                with state_lock:
                    state["trades"][ticker] = True
                    _save_state(state)
                _monitor_bracket(ib, contract, trades, direction_confirmed,
                                 bracket, orng, ticker, currency, session_end)
                return

        time.sleep(polls_per_sec)

    send_message(f"😴 {ticker} IB — no clean setup by {US_IB_WINDOW_END} NL, standing aside.")


# ── VWAP session runner ───────────────────────────────────────────────────────

def run_ticker_vwap(pick: dict, ib, state: dict, state_lock: threading.Lock) -> None:
    """VWAP Reversion session for one ticker.

    Polls 1-min bars from open, computes running VWAP, and enters when
    price deviates >= vwap_min_deviation% away with a reversal confirmation.
    """
    ticker      = pick["ticker"]
    signal      = pick.get("signal", "HOLD")
    direction   = _SIGNAL_TO_DIRECTION.get(signal, "skip")
    currency    = pick.get("currency", "USD")
    session_end = _today_at(US_SESSION_END)
    window_end  = _today_at(US_VWAP_WINDOW_END)

    if direction == "skip":
        send_message(f"😴 {ticker} — HOLD signal, skipping VWAP today.")
        return

    with state_lock:
        if state["trades"].get(ticker):
            return

    cfg_vwap    = VWAPConfig.from_params(config.INTRADAY_PARAMS)
    polls_per_sec = config.INTRADAY_PARAMS.poll_interval_sec
    contract    = ibkr_connector.get_contract(ticker, "SMART", currency)

    # Fetch all bars since open to seed the VWAP history
    _wait_until(_today_at(US_OPEN))
    seed_raw = ib.reqHistoricalData(
        contract, endDateTime="", durationStr="3600 S",
        barSizeSetting="1 min", whatToShow="TRADES",
        useRTH=True, formatDate=1,
    )
    bar_history: list[Bar] = [
        Bar(
            t=b.date.astimezone(NL).strftime("%H:%M"),
            open=b.open, high=b.high, low=b.low, close=b.close, volume=float(b.volume),
        )
        for b in (seed_raw or [])
        if b.date.astimezone(NL).strftime("%H:%M") >= US_OPEN
    ]

    send_message(
        f"📡 VWAP watching <b>{ticker}</b>  ·  "
        f"deviation gate {cfg_vwap.min_deviation*100:.1f}%  "
        f"bias: {'auto' if direction=='skip' else direction.upper()}"
    )

    # ── Poll loop ─────────────────────────────────────────────────────────
    seen_last_t: str | None = bar_history[-1].t if bar_history else None

    while datetime.now(NL) < window_end:
        bar_raw = ibkr_connector.get_latest_closed_1min_bar(ib, contract)
        if bar_raw is None:
            time.sleep(polls_per_sec)
            continue

        bar_t = bar_raw.date.astimezone(NL).strftime("%H:%M")
        if bar_t == seen_last_t:
            time.sleep(polls_per_sec)
            continue
        seen_last_t = bar_t

        bar = Bar(
            t=bar_t, open=bar_raw.open, high=bar_raw.high,
            low=bar_raw.low, close=bar_raw.close, volume=float(bar_raw.volume),
        )
        bar_history.append(bar)

        if len(bar_history) < cfg_vwap.warmup_bars + 1:
            time.sleep(polls_per_sec)
            continue

        # bias: "auto" = fade whichever side is overextended
        # "long"/"short" = only fade price that moved against the signal direction
        vwap_bias = "auto" if direction == "skip" else direction
        setup_dir, entry_price, vwap_level = detect_vwap_setup(
            bar_history, cfg_vwap, bias=vwap_bias
        )

        if setup_dir is None:
            time.sleep(polls_per_sec)
            continue

        # Build bracket levels from the setup
        import statistics as _stats
        atr_bars   = bar_history[-cfg_vwap.min_atr_bars:]
        atr_est    = _stats.mean(b.high - b.low for b in atr_bars if b.high > b.low) or 0.01
        slip       = cfg_vwap.slippage_pct * entry_price

        if setup_dir == "long":
            entry_f  = entry_price + slip
            stop     = round(entry_f - cfg_vwap.stop_atr_mult * atr_est, 2)
            risk     = entry_f - stop
            tp       = round(entry_f + cfg_vwap.tp_r_multiple * risk, 2)
        else:
            entry_f  = entry_price - slip
            stop     = round(entry_f + cfg_vwap.stop_atr_mult * atr_est, 2)
            risk     = stop - entry_f
            tp       = round(entry_f - cfg_vwap.tp_r_multiple * risk, 2)

        entry_f  = round(entry_f, 2)
        if risk <= 0:
            time.sleep(polls_per_sec)
            continue

        from orb_core import position_size_usd, OpeningRange
        qty = position_size_usd(_risk_usd(), risk)
        if qty == 0:
            send_message(f"⚠️ {ticker} VWAP qty=0 (stop too wide) — skipping.")
            time.sleep(polls_per_sec)
            continue

        action = "BUY" if setup_dir == "long" else "SELL"
        trades = ibkr_connector.place_bracket_order(
            ib, contract, action, qty, entry_f, tp, stop
        )
        send_message(
            f"✅ VWAP {'Long' if setup_dir=='long' else 'Short'} <b>{ticker}</b>  "
            f"entry {entry_f:.2f}  SL {stop:.2f}  TP {tp:.2f}  "
            f"qty {qty}  VWAP={vwap_level:.2f}"
        )
        with state_lock:
            state["trades"][ticker] = True
            _save_state(state)

        # Reuse ORB monitoring — TP/SL/re-entry work the same way
        # Build a stub OpeningRange so _monitor_bracket re-entry check works
        fake_orng = OpeningRange(ticker=ticker, high=vwap_level, low=vwap_level - risk)
        bracket   = {"entry": entry_f, "take_profit": tp, "stop": stop, "qty": qty}
        _monitor_bracket(ib, contract, trades, setup_dir,
                         bracket, fake_orng, ticker, currency, session_end)
        return

    send_message(f"😴 {ticker} VWAP — no setup by {US_VWAP_WINDOW_END} NL.")


# ── Dispatcher ────────────────────────────────────────────────────────────────

_DISPATCH = {
    "ORB":  run_ticker_orb,
    "IB":   run_ticker_ib,
    "VWAP": run_ticker_vwap,
}


def main() -> None:
    print(f"\n{'='*58}")
    print(f"  Signal Mesh Day — Live Engine")
    print(f"  {datetime.now(NL).strftime('%Y-%m-%d %H:%M')} NL")
    print(f"  LIVE_TRADING = {config.LIVE_TRADING}")
    print(f"{'='*58}\n")

    # Load watchlist and read strategy chosen by regime detection
    from orb_strategy import _load_watchlist
    picks = _load_watchlist()

    # Read strategy from watchlist (defaults to ORB for backward compat)
    today_path = Path(config.WATCHLIST_DIR) / f"watchlist_{datetime.now(NL).strftime('%Y%m%d')}.json"
    strategy   = "ORB"
    if today_path.exists():
        with open(today_path) as f:
            strategy = json.load(f).get("strategy", "ORB").upper()

    if strategy == "SIT_OUT":
        send_message("😴 Regime says SIT_OUT — no trades today.")
        return

    runner = _DISPATCH.get(strategy, run_ticker_orb)
    print(f"  Strategy today: {strategy}  (runner: {runner.__name__})")

    actionable = [
        p for p in picks
        if _SIGNAL_TO_DIRECTION.get(p.get("signal", "HOLD"), "skip") != "skip"
    ]
    if not actionable:
        send_message("😴 No actionable picks in today's watchlist (all HOLD).")
        return

    send_message(
        f"🚀 <b>Live Engine — {strategy}</b>  ·  {len(actionable)} pick(s)\n"
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

    state      = _load_state()
    state_lock = threading.Lock()

    threads: list[threading.Thread] = []
    for pick in actionable[:config.MAX_CONCURRENT_POSITIONS]:
        t = threading.Thread(
            target=runner,
            args=(pick, ib, state, state_lock),
            name=f"{strategy}-{pick['ticker']}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        print(f"  [{pick['ticker']}] {strategy} thread started")

    for t in threads:
        t.join()

    print("\n  Running EOD safety flatten check...")
    _eod_safety_flatten(ib, actionable)

    ib.disconnect()
    send_message(f"✅ {strategy} session complete — all positions flat.")
    print("\nDisconnected cleanly.")


if __name__ == "__main__":
    main()
