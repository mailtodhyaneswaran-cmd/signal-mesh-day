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
    ORBConfig, Bar, OpeningRange, capture_opening_range,
    detect_breakout, confirm_retest, build_bracket, position_size_usd,
)
from profiles import get_profile
from telegram_notify import send_message
from vwap_strategy import VWAPConfig, compute_vwap, detect_vwap_setup

# Re-use all shared helpers from orb_strategy (don't duplicate)
from orb_strategy import (
    _SIGNAL_TO_DIRECTION,
    _risk_usd, _max_notional, _today_at, _wait_until,
    _load_state, _save_state, _force_fill,
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

def run_ticker_ib(
    pick:            dict,
    ib,
    state:           dict,
    state_lock:      threading.Lock,
    profile:         dict | None = None,
    account_summary: dict | None = None,
) -> None:
    """60-min Initial Balance Breakout session for one ticker.

    Waits for the first 60 minutes of trading to close (10:30 ET),
    captures the IB range, then watches for a breakout + retest using
    the same orb_core primitives as ORB — just with a wider range.
    """
    if profile is None:
        profile = get_profile(config.PROFILE)

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
    cfg.range_minutes = len(ib_bars)
    cfg.rvol_mode     = "disabled"
    cfg.rvol_min      = 0.0

    # Apply profile overrides
    min_range_override = profile.get("min_range_pct_override")
    if min_range_override is not None:
        cfg.min_range_pct = min_range_override
    require_retest = profile.get("require_retest")
    if require_retest is not None:
        cfg.require_retest = require_retest

    orng = capture_opening_range(ib_bars, cfg, ticker)
    if orng is None:
        msg = f"😴 {ticker} IB range too thin — {'forcing entry' if profile.get('force_trade') else 'skipping'}"
        print(msg); send_message(msg)
        if not profile.get("force_trade"):
            return
        orng = OpeningRange(ticker=ticker,
                            high=max(b.high for b in ib_bars),
                            low=min(b.low  for b in ib_bars))

    send_message(
        f"📊 <b>{ticker}</b> IB range: {orng.low:.2f}–{orng.high:.2f} {currency}"
        f"  ·  bias: {direction.upper()}"
    )

    # ── Poll 1-min bars for breakout → retest ────────────────────────────
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

                if not cfg.require_retest:
                    bracket = build_bracket(orng, lvl, direction_confirmed, cfg,
                                            _risk_usd(account_summary), _max_notional(account_summary))
                    if bracket["qty"] == 0:
                        send_message(f"⚠️ {ticker} IB qty=0 — skipping.")
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

                send_message(
                    f"{'📈' if direction=='long' else '📉'} <b>{ticker}</b> IB "
                    f"broke {'above' if direction=='long' else 'below'} {lvl:.2f} "
                    f"— watching for retest..."
                )
        else:
            retest = confirm_retest(orng, bar, cfg, direction_confirmed)
            if retest is False:
                if cfg.require_retest:
                    send_message(f"⚠️ {ticker} IB failed retest — skipping today.")
                    return
                direction_confirmed = None
            elif retest is True:
                entry_price = orng.high if direction_confirmed == "long" else orng.low
                bracket     = build_bracket(orng, entry_price, direction_confirmed, cfg,
                                            _risk_usd(account_summary), _max_notional(account_summary))
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

    if profile.get("force_trade"):
        _force_fill(ticker, ib, contract, orng, direction, cfg, profile,
                    account_summary, state, state_lock, session_end, currency)
    else:
        send_message(f"😴 {ticker} IB — no clean setup by {US_IB_WINDOW_END} NL, standing aside.")


# ── VWAP session runner ───────────────────────────────────────────────────────

def run_ticker_vwap(
    pick:            dict,
    ib,
    state:           dict,
    state_lock:      threading.Lock,
    profile:         dict | None = None,
    account_summary: dict | None = None,
) -> None:
    """VWAP Reversion session for one ticker.

    Polls 1-min bars from open, computes running VWAP, and enters when
    price deviates >= vwap_min_deviation% away with a reversal confirmation.
    """
    if profile is None:
        profile = get_profile(config.PROFILE)

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

        qty = position_size_usd(_risk_usd(account_summary), risk,
                                _max_notional(account_summary), entry_f)
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

        fake_orng = OpeningRange(ticker=ticker, high=vwap_level, low=vwap_level - risk)
        bracket   = {"entry": entry_f, "take_profit": tp, "stop": stop, "qty": qty}
        _monitor_bracket(ib, contract, trades, setup_dir,
                         bracket, fake_orng, ticker, currency, session_end)
        return

    if profile.get("force_trade"):
        # Use the last known VWAP level (or mid of bar_history) for the synthetic range
        last_bars  = bar_history[-5:] if len(bar_history) >= 5 else bar_history
        fake_high  = max(b.high for b in last_bars) if last_bars else 0
        fake_low   = min(b.low  for b in last_bars) if last_bars else 0
        fake_orng  = OpeningRange(ticker=ticker, high=fake_high, low=fake_low)
        cfg_orb    = ORBConfig.from_params(config.INTRADAY_PARAMS)
        _force_fill(ticker, ib, contract, fake_orng, direction if direction != "skip" else "long",
                    cfg_orb, profile, account_summary, state, state_lock, session_end, currency)
    else:
        send_message(f"😴 {ticker} VWAP — no setup by {US_VWAP_WINDOW_END} NL.")


# ── Dispatcher ────────────────────────────────────────────────────────────────

_DISPATCH = {
    "ORB":  run_ticker_orb,
    "IB":   run_ticker_ib,
    "VWAP": run_ticker_vwap,
}


def main() -> None:
    profile = get_profile(config.PROFILE)

    print(f"\n{'='*58}")
    print(f"  Signal Mesh Day — Live Engine")
    print(f"  {datetime.now(NL).strftime('%Y-%m-%d %H:%M')} NL")
    print(f"  LIVE_TRADING = {config.LIVE_TRADING}  |  PROFILE = {config.PROFILE}")
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
        if not profile.get("allow_sit_out", True):
            strategy = "VWAP"
            send_message(f"⚠️ Regime says SIT_OUT but profile={config.PROFILE} forces VWAP.")
        else:
            send_message("😴 Regime says SIT_OUT — no trades today.")
            return

    runner = _DISPATCH.get(strategy, run_ticker_orb)
    print(f"  Strategy today: {strategy}  (runner: {runner.__name__})")

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
    for pick in actionable[:max_picks]:
        t = threading.Thread(
            target=runner,
            args=(pick, ib, state, state_lock),
            kwargs={"profile": profile, "account_summary": account_summary},
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
