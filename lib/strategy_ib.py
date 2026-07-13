"""
strategy_ib.py — 60-min Initial Balance (IB) Breakout strategy.

The Initial Balance is the high/low range of the first 60 minutes after
open (9:30–10:30 ET). Once that range is established, a close outside
it — confirmed by a retest — signals a directional move.

Differences from ORB (5-min range):
  - range_minutes = 60  (configurable via config.ib_range_minutes)
  - rvol_mode = "disabled"  — no premarket RVOL gate; all signals are post-open
  - Fires once per session after 10:30 ET (not immediately at 9:30)
  - Wider stop (opposite IB edge) → fewer shares, same dollar risk

IB is an Opening Range Breakout variant with a wider window, so it reuses
strategy_orb.py's breakout/retest/bracket primitives directly rather than
duplicating them.

All IBKR access goes through ibkr_connector.py. All engine plumbing shared
with the other strategies lives in session_runtime.py.

Called by bin/live_engine.py via run(); never run directly.
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402

import threading
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import config
import ibkr_connector
from profiles import get_profile
from session_runtime import (
    _SIGNAL_TO_DIRECTION,
    _risk_usd, _max_notional, _wait_until,
    _et_today_at, _et_nl_hhmm,
    ET_OPEN, ET_IB_RANGE_END, ET_IB_WINDOW_END, ET_SESSION_END,
    _save_state, _force_fill, _monitor_bracket,
)
from strategy_orb import (
    ORBConfig, Bar, OpeningRange,
    capture_opening_range, detect_breakout, confirm_retest, build_bracket,
)
from telegram_notify import send_message

NL = ZoneInfo("Europe/Amsterdam")

IB_DEFAULT_RANGE_MINUTES = 60


def _ib_config(params) -> ORBConfig:
    """Build an ORBConfig suitable for IB: 60-min range, RVOL disabled."""
    cfg = ORBConfig.from_params(params)
    cfg.range_minutes = getattr(params, "ib_range_minutes", IB_DEFAULT_RANGE_MINUTES)
    cfg.rvol_mode     = "disabled"   # no premarket RVOL gate for IB
    cfg.rvol_min      = 0.0          # always passes the live breakout check
    return cfg


# ── Live session runner (single ticker, one thread) ──────────────────────────

def run(
    pick:            dict,
    ib,
    state:           dict,
    state_lock:      threading.Lock,
    profile:         dict | None = None,
    account_summary: dict | None = None,
    *,
    window_end_override=None,
    allow_force_fill: bool = True,
    stagger_index:   int = 0,
) -> bool:
    """Run the full 60-min IB session for one watchlist pick (own thread).

    Waits for the first 60 minutes of trading to close (10:30 ET), captures
    the IB range, then watches for a breakout + retest using strategy_orb's
    primitives — just with a wider range. Called by bin/live_engine.py.

    window_end_override / allow_force_fill: see strategy_orb.run().
    Returns True if a trade was placed (real or forced), else False.
    """
    if profile is None:
        profile = get_profile(config.PROFILE)

    ticker        = pick["ticker"]
    signal        = pick.get("signal", "HOLD")
    direction     = _SIGNAL_TO_DIRECTION.get(signal, "skip")
    currency      = pick.get("currency", "USD")
    session_end   = _et_today_at(ET_SESSION_END)
    window_end    = window_end_override or _et_today_at(ET_IB_WINDOW_END)
    open_nl       = _et_nl_hhmm(ET_OPEN)             # 09:30 ET in NL
    ib_range_nl   = _et_nl_hhmm(ET_IB_RANGE_END)     # 10:30 ET in NL

    if direction == "skip":
        send_message(f"😴 {ticker} — HOLD signal, skipping IB today.")
        return False

    with state_lock:
        if state["trades"].get(ticker):
            return False

    send_message(
        f"🔔 IB watching <b>{ticker}</b>  ·  bias: {direction.upper()}\n"
        f"  Waiting for 60-min IB range to close at {ib_range_nl} NL..."
    )

    # ── Wait for 60-min IB range to close ────────────────────────────────
    _wait_until(_et_today_at(ET_IB_RANGE_END))

    # Give IBKR 90s to finalize the 10:30 ET bar before any request.
    time.sleep(90)

    # Stagger so threads don't all reach get_historical_bars simultaneously.
    if stagger_index > 0:
        time.sleep(stagger_index * 5)

    contract = ibkr_connector.get_contract(ticker, "SMART", currency)

    # 8-hour duration matches what reliably returns bars for this contract type.
    ib_bars_raw = ibkr_connector.get_historical_bars(
        ib, contract, "28800 S", "1 min", use_rth=True,
    )

    if not ib_bars_raw:
        msg = (f"⚠️ {ticker}: no IB range bars available. "
               f"Contract: {contract}. Check TWS market data subscriptions.")
        print(msg); send_message(msg)
        # try_luck: bars unavailable → synthetic force-fill rather than giving up
        if profile.get("force_trade") and allow_force_fill and not state["trades"].get(ticker):
            last_bar = ibkr_connector.get_latest_closed_1min_bar(ib, contract)
            if last_bar:
                p = last_bar.close
                synth_orng = OpeningRange(ticker=ticker,
                                          high=round(p * 1.005, 2),
                                          low=round(p * 0.995, 2))
                print(f"  [{ticker}] IB bars unavailable — forcing synthetic entry at {p:.2f}")
                _force_fill(ticker, ib, contract, synth_orng, direction,
                            _ib_config(config.INTRADAY_PARAMS),
                            profile, account_summary, state, state_lock, session_end, currency)
                return True
        return False

    # Filter to bars from 09:30–10:30 ET (converted to NL for the bar timestamps)
    ib_bars = [
        Bar(
            t=b.date.astimezone(NL).strftime("%H:%M"),
            open=b.open, high=b.high, low=b.low, close=b.close, volume=float(b.volume),
        )
        for b in ib_bars_raw
        if open_nl <= b.date.astimezone(NL).strftime("%H:%M") < ib_range_nl
    ]
    if len(ib_bars) < 10:
        msg = f"⚠️ {ticker}: only {len(ib_bars)} IB bars — skipping."
        print(msg); send_message(msg)
        return False

    # Build ORBConfig tuned for IB (60-min range, RVOL disabled)
    cfg = _ib_config(config.INTRADAY_PARAMS)
    cfg.range_minutes = len(ib_bars)

    # Apply profile overrides
    min_range_override = profile.get("min_range_pct_override")
    if min_range_override is not None:
        cfg.min_range_pct = min_range_override
    require_retest = profile.get("require_retest")
    if require_retest is not None:
        cfg.require_retest = require_retest

    orng = capture_opening_range(ib_bars, cfg, ticker)
    if orng is None:
        will_force = profile.get("force_trade") and allow_force_fill
        msg = f"😴 {ticker} IB range too thin — {'forcing entry' if will_force else 'skipping'}"
        print(msg); send_message(msg)
        if not will_force:
            return False
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
                        return False
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
                    return True

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
                    return False
                direction_confirmed = None
            elif retest is True:
                entry_price = orng.high if direction_confirmed == "long" else orng.low
                bracket     = build_bracket(orng, entry_price, direction_confirmed, cfg,
                                            _risk_usd(account_summary), _max_notional(account_summary))
                if bracket["qty"] == 0:
                    send_message(f"⚠️ {ticker} IB qty=0 (stop too wide) — skipping.")
                    return False
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
                return True

        time.sleep(polls_per_sec)

    if profile.get("force_trade") and allow_force_fill:
        _force_fill(ticker, ib, contract, orng, direction, cfg, profile,
                    account_summary, state, state_lock, session_end, currency)
        return True
    send_message(f"😴 {ticker} IB — no clean setup by {window_end.strftime('%H:%M')} NL, standing aside.")
    return False


# ── Backtest simulation ────────────────────────────────────────────────────────

def simulate_ib_session(
    bars:         list[Bar],
    capital_usd:  float,
    params,
    bias:         str  = "auto",
    verbose:      bool = False,
):
    """Replay one IB session. Thin wrapper over strategy_orb.simulate_session.

    bias = "auto": take whichever side breaks the IB range first.
    Returns strategy_orb.TradeResult or None.
    """
    from strategy_orb import simulate_session

    cfg = _ib_config(params)

    if len(bars) < cfg.range_minutes + 1:
        if verbose:
            print(f"      Not enough bars for {cfg.range_minutes}-min IB ({len(bars)} bars)")
        return None

    opening = bars[:cfg.range_minutes]
    post    = bars[cfg.range_minutes:]

    return simulate_session(
        opening_bars = opening,
        post_bars    = post,
        capital_usd  = capital_usd,
        cfg          = cfg,
        bias         = bias,
        verbose      = verbose,
    )
