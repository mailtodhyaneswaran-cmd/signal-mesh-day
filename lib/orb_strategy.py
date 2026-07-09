"""
orb_strategy.py — live 5-min Opening Range Breakout engine.

Implements Strategy (strategy_base.py) and the full session runner that
loads watchlist_YYYYMMDD.json, gates direction per the mesh signal, and
executes ORB via orb_core primitives through ibkr_connector.

Signal → Direction mapping:
  BUY  → long  (upside breakout only)
  SELL → short (downside breakout only)
  HOLD → skip

Run via live_engine.py (recommended) or directly:
  python lib/orb_strategy.py
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
import ibkr_connector
from orb_core import ORBConfig, Bar, OpeningRange, capture_opening_range, detect_breakout, confirm_retest, build_bracket, position_size_usd
from profiles import get_profile
from strategy_base import StrategySignal, STRATEGY_REGISTRY
from telegram_notify import send_message

NL = ZoneInfo("Europe/Amsterdam")

# Signal → direction mapping
_SIGNAL_TO_DIRECTION = {"BUY": "long", "SELL": "short", "HOLD": "skip"}

# US session window (NL / Amsterdam time)
US_OPEN          = "15:30"
US_OPEN_END      = "15:35"   # opening range closes at this time
US_ORB_WINDOW_END = "19:00"  # 13:00 ET — stop looking for ORB setup
US_SESSION_END   = "22:00"   # hard EOD flatten


# ── Strategy Protocol implementation ─────────────────────────────────────────

class ORBStrategy:
    """5-min Opening Range Breakout strategy.

    evaluate() is called by the live engine with the intraday bar list
    accumulated so far.  Returns StrategySignal; engine handles sizing +
    bracket order placement.
    """
    name = "orb"

    def evaluate(
        self,
        bars:   list[Bar],
        bias:   str,
        params,
    ) -> StrategySignal:
        """Evaluate ORB signal from accumulated intraday bars.

        bars[0..range_minutes-1]  = opening range bars
        bars[range_minutes..]     = post-open bars to scan for breakout/retest

        Returns StrategySignal(direction="skip") when no setup is found.
        """
        cfg = ORBConfig.from_params(params)

        if len(bars) < cfg.range_minutes:
            return StrategySignal(direction="skip")

        opening = bars[:cfg.range_minutes]
        post    = bars[cfg.range_minutes:]

        orng = capture_opening_range(opening, cfg)
        if orng is None:
            return StrategySignal(direction="skip")

        direction_confirmed = None
        for bar in post:
            if direction_confirmed is None:
                if detect_breakout(orng, bar, bias):
                    direction_confirmed = bias
                continue

            retest = confirm_retest(orng, bar, cfg, direction_confirmed)
            if retest is False:
                return StrategySignal(direction="skip")   # failed retest
            if retest is True:
                entry = orng.high if direction_confirmed == "long" else orng.low
                bracket = build_bracket(
                    orng, entry, direction_confirmed, cfg,
                    risk_usd=_risk_usd(),
                )
                if bracket["qty"] == 0:
                    return StrategySignal(direction="skip")
                return StrategySignal(
                    direction = direction_confirmed,
                    entry     = bracket["entry"],
                    stop      = bracket["stop"],
                    target    = bracket["take_profit"],
                    qty       = bracket["qty"],
                )

        return StrategySignal(direction="skip")


STRATEGY_REGISTRY["orb"] = ORBStrategy


# ── Helpers ───────────────────────────────────────────────────────────────────

def _risk_usd(account_summary: dict | None = None) -> float:
    """Per-trade risk budget in USD.

    Uses live IBKR available_funds when account_summary is provided (preferred).
    Falls back to config.HOUSE_MONEY_EUR × RISK_PER_TRADE_PCT × EUR/USD.
    """
    if account_summary and account_summary.get("available_funds", 0) > 0:
        return account_summary["available_funds"] * config.RISK_PER_TRADE_PCT
    risk_eur = config.HOUSE_MONEY_EUR * config.RISK_PER_TRADE_PCT
    eurusd   = ibkr_connector.get_eurusd_rate()
    return risk_eur * eurusd


def _max_notional(account_summary: dict | None = None) -> float:
    """Max notional per trade in USD (0 = no cap).

    Uses live available_funds × MAX_NOTIONAL_PER_TRADE_PCT from config.
    Returns 0 if the config knob is absent or account data unavailable.
    """
    pct = getattr(config.INTRADAY_PARAMS, "max_notional_per_trade_pct", 0.0)
    if pct <= 0:
        return 0.0
    if account_summary and account_summary.get("available_funds", 0) > 0:
        return account_summary["available_funds"] * pct
    return config.HOUSE_MONEY_EUR * ibkr_connector.get_eurusd_rate() * pct


def _today_at(hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.now(NL).replace(hour=h, minute=m, second=0, microsecond=0)


def _wait_until(target: datetime) -> None:
    while True:
        remaining = (target - datetime.now(NL)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 30))


def _load_watchlist(wait_minutes: int = 30) -> list[dict]:
    """Load today's watchlist_YYYYMMDD.json from WATCHLIST_DIR.

    If the file doesn't exist yet (screener still running), waits up to
    wait_minutes before raising FileNotFoundError.
    """
    today = datetime.now(NL).strftime("%Y%m%d")
    path  = Path(config.WATCHLIST_DIR) / f"watchlist_{today}.json"
    deadline = time.time() + wait_minutes * 60
    while not path.exists():
        remaining = deadline - time.time()
        if remaining <= 0:
            raise FileNotFoundError(
                f"Watchlist not found after waiting {wait_minutes}m: {path}\n"
                f"Run the Phase 1 screener first."
            )
        wait = min(30, remaining)
        print(f"  [watchlist] File not ready yet — retrying in {wait:.0f}s "
              f"({remaining/60:.1f}m left)...")
        time.sleep(wait)
    with open(path) as f:
        data = json.load(f)
    return data.get("picks", [])


def _load_state() -> dict:
    today = datetime.now(NL).strftime("%Y-%m-%d")
    state = {"date": today, "trades": {}}
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE) as f:
            saved = json.load(f)
        if saved.get("date") == today:
            state.update(saved)
    return state


def _save_state(state: dict) -> None:
    with open(config.STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Force-fill helper ────────────────────────────────────────────────────────

def _force_fill(
    ticker:          str,
    ib,
    contract,
    orng:            OpeningRange,
    direction:       str,
    cfg:             ORBConfig,
    profile:         dict,
    account_summary: dict | None,
    state:           dict,
    state_lock:      threading.Lock,
    session_end:     datetime,
    currency:        str,
) -> None:
    """Place a synthetic bracket order when nothing genuine triggered by deadline.

    Uses the opening range for SL/TP geometry and the last bar price as entry.
    Tagged in Telegram with profile["telegram_tag"] so it's never mistaken for
    a real signal.
    """
    bar_raw    = ibkr_connector.get_latest_closed_1min_bar(ib, contract)
    last_price = bar_raw.close if bar_raw else orng.mid
    risk_mult  = profile.get("force_fill_risk_multiplier", 1.0)
    risk       = _risk_usd(account_summary) * risk_mult
    max_n      = _max_notional(account_summary)
    bracket    = build_bracket(orng, last_price, direction, cfg, risk, max_n)

    if bracket["qty"] == 0:
        send_message(f"⚠️ {ticker} force-fill: qty=0 after sizing — skipping.")
        return

    tag    = profile.get("telegram_tag") or "FORCED"
    action = "BUY" if direction == "long" else "SELL"
    trades = ibkr_connector.place_bracket_order(
        ib, contract, action, bracket["qty"],
        bracket["entry"], bracket["take_profit"], bracket["stop"],
    )
    send_message(
        f"⚠️ <b>{ticker}</b> [{tag}]\n"
        f"  {'Long' if direction == 'long' else 'Short'}  "
        f"entry {bracket['entry']:.2f}  SL {bracket['stop']:.2f}  "
        f"TP {bracket['take_profit']:.2f}  qty {bracket['qty']}  "
        f"exposure ${bracket['entry'] * bracket['qty']:.0f}"
    )
    with state_lock:
        state["trades"][ticker] = True
        _save_state(state)
    _monitor_bracket(ib, contract, trades, direction, bracket, orng, ticker, currency, session_end)


# ── Session runner (single ticker) ───────────────────────────────────────────

def run_ticker_orb(
    pick:            dict,
    ib,
    state:           dict,
    state_lock:      threading.Lock,
    profile:         dict | None = None,
    account_summary: dict | None = None,
) -> None:
    """Run the full ORB session for one watchlist pick (runs in its own thread)."""
    if profile is None:
        profile = get_profile(config.PROFILE)

    ticker    = pick["ticker"]
    signal    = pick.get("signal", "HOLD")
    direction = _SIGNAL_TO_DIRECTION.get(signal, "skip")
    currency  = pick.get("currency", "USD")
    cfg       = ORBConfig.from_params(config.INTRADAY_PARAMS)
    session_end = _today_at(US_SESSION_END)
    window_end  = _today_at(US_ORB_WINDOW_END)

    # ── Apply profile overrides to cfg ────────────────────────────────────────
    live_rvol_gate = profile.get("live_rvol_gate_override")
    if live_rvol_gate is not None:
        cfg.rvol_min = live_rvol_gate

    min_range_override = profile.get("min_range_pct_override")
    if min_range_override is not None:
        cfg.min_range_pct = min_range_override

    require_retest = profile.get("require_retest")
    if require_retest is not None:
        cfg.require_retest = require_retest

    # ── Conviction-modulated live RVOL gate (paper experiment) ───────────────
    p = config.INTRADAY_PARAMS
    if getattr(p, "conviction_rvol_gate_enabled", False) and live_rvol_gate is None:
        conv  = float(pick.get("confidence", 0.0))
        base  = getattr(p, "conviction_rvol_base",  p.orb_rvol_gate)
        span  = getattr(p, "conviction_rvol_span",  0.5)
        floor = getattr(p, "conviction_rvol_floor", 1.0)
        cap   = getattr(p, "conviction_rvol_cap",   2.5)
        adj   = (0.5 - conv) * span
        cfg.rvol_min = max(floor, min(base + adj, cap))
        send_message(
            f"Rvol gate {ticker}: {cfg.rvol_min:.2f}x "
            f"(conviction {conv:.0%}, base {base})"
        )

    if direction == "skip":
        send_message(f"😴 {ticker} — HOLD signal, skipping ORB today.")
        return

    # Thread-safe MAX_TRADES_PER_SYMBOL_PER_DAY gate
    with state_lock:
        if state["trades"].get(ticker):
            print(f"{ticker}: trade already taken today — skipping.")
            return

    send_message(
        f"🔔 Watching <b>{ticker}</b> for ORB  ·  bias: {direction.upper()}\n"
        f"  Waiting for {US_OPEN} candle close..."
    )

    # ── Wait for opening range to form ───────────────────────────────────────
    _wait_until(_today_at(US_OPEN_END))

    contract    = ibkr_connector.get_contract(ticker, "SMART", currency)
    opening_bar = None
    for _ in range(12):
        opening_bar = ibkr_connector.get_opening_range_bar(ib, contract, US_OPEN)
        if opening_bar is not None:
            break
        time.sleep(5)

    if opening_bar is None:
        msg = f"⚠️ {ticker}: opening candle not available — skipping."
        print(msg); send_message(msg)
        return

    orng_bars = [Bar(
        t      = opening_bar.date.astimezone(NL).strftime("%H:%M"),
        open   = opening_bar.open,
        high   = opening_bar.high,
        low    = opening_bar.low,
        close  = opening_bar.close,
        volume = opening_bar.volume,
    )]

    orng = capture_opening_range(orng_bars, cfg, ticker)
    if orng is None:
        msg = (f"😴 {ticker} range too thin "
               f"({opening_bar.low:.2f}–{opening_bar.high:.2f}) — "
               f"{'forcing entry at last price' if profile.get('force_trade') else 'skipping'}")
        print(msg); send_message(msg)
        if not profile.get("force_trade"):
            return
        # Build a synthetic range from the opening bar so force-fill has geometry
        orng = OpeningRange(ticker=ticker,
                            high=opening_bar.high, low=opening_bar.low)

    send_message(
        f"📊 <b>{ticker}</b> opening range: {orng.low:.2f}–{orng.high:.2f} {currency}  "
        f"·  bias: {direction.upper()}"
    )

    # ── Poll for breakout → retest ────────────────────────────────────────────
    direction_confirmed = None
    polls_per_sec       = config.INTRADAY_PARAMS.poll_interval_sec

    while datetime.now(NL) < window_end:
        bar_raw = ibkr_connector.get_latest_closed_1min_bar(ib, contract)
        if bar_raw is None:
            time.sleep(polls_per_sec)
            continue

        bar = Bar(
            t      = bar_raw.date.astimezone(NL).strftime("%H:%M"),
            open   = bar_raw.open,
            high   = bar_raw.high,
            low    = bar_raw.low,
            close  = bar_raw.close,
            volume = bar_raw.volume,
        )

        if direction_confirmed is None:
            rv = ibkr_connector.get_rvol(ib, contract, bar.volume)
            orng.rvol = rv
            if detect_breakout(orng, bar, direction):
                if rv < cfg.rvol_min:
                    side = "above" if direction == "long" else "below"
                    lvl  = orng.high if direction == "long" else orng.low
                    send_message(
                        f"⚠️ {ticker} breakout {side} {lvl:.2f} — low RVOL ({rv:.1f}x), skipping."
                    )
                    time.sleep(polls_per_sec)
                    continue

                direction_confirmed = direction

                # No retest required — enter immediately on breakout
                if not cfg.require_retest:
                    entry_price = orng.high if direction_confirmed == "long" else orng.low
                    bracket = build_bracket(orng, entry_price, direction_confirmed, cfg,
                                            _risk_usd(account_summary), _max_notional(account_summary))
                    if bracket["qty"] == 0:
                        send_message(f"⚠️ {ticker} setup skipped — qty=0.")
                        return
                    action = "BUY" if direction_confirmed == "long" else "SELL"
                    trades = ibkr_connector.place_bracket_order(
                        ib, contract, action, bracket["qty"],
                        bracket["entry"], bracket["take_profit"], bracket["stop"],
                    )
                    send_message(
                        f"✅ {'Long' if direction_confirmed == 'long' else 'Short'} <b>{ticker}</b>  "
                        f"entry {bracket['entry']:.2f}  SL {bracket['stop']:.2f}  "
                        f"TP {bracket['take_profit']:.2f}  qty {bracket['qty']}  "
                        f"exposure ${bracket['entry'] * bracket['qty']:.0f}"
                    )
                    with state_lock:
                        state["trades"][ticker] = True
                        _save_state(state)
                    _monitor_bracket(ib, contract, trades, direction_confirmed,
                                     bracket, orng, ticker, currency, session_end)
                    return

                lvl_str = orng.high if direction == "long" else orng.low
                send_message(
                    f"{'📈' if direction == 'long' else '📉'} <b>{ticker}</b> "
                    f"broke {'above' if direction == 'long' else 'below'} {lvl_str:.2f}  "
                    f"RVOL {rv:.1f}x — watching for retest..."
                )
        else:
            retest = confirm_retest(orng, bar, cfg, direction_confirmed)
            if retest is False:
                if cfg.require_retest:
                    send_message(f"⚠️ {ticker} failed retest — skipping today.")
                    return
                # require_retest disabled: reset and keep watching for the next breakout
                direction_confirmed = None
            elif retest is True:
                entry_price = orng.high if direction_confirmed == "long" else orng.low
                bracket     = build_bracket(orng, entry_price, direction_confirmed, cfg,
                                            _risk_usd(account_summary), _max_notional(account_summary))
                if bracket["qty"] == 0:
                    send_message(f"⚠️ {ticker} setup skipped — qty=0 (stop too wide for risk budget).")
                    return

                action = "BUY" if direction_confirmed == "long" else "SELL"
                trades = ibkr_connector.place_bracket_order(
                    ib, contract, action, bracket["qty"],
                    bracket["entry"], bracket["take_profit"], bracket["stop"],
                )
                send_message(
                    f"✅ {'Long' if direction_confirmed == 'long' else 'Short'} <b>{ticker}</b>  "
                    f"entry {bracket['entry']:.2f}  SL {bracket['stop']:.2f}  "
                    f"TP {bracket['take_profit']:.2f}  qty {bracket['qty']}  "
                    f"exposure ${bracket['entry'] * bracket['qty']:.0f}"
                )
                with state_lock:
                    state["trades"][ticker] = True
                    _save_state(state)
                _monitor_bracket(ib, contract, trades, direction_confirmed,
                                 bracket, orng, ticker, currency, session_end)
                return

        time.sleep(polls_per_sec)

    # ── Window closed ─────────────────────────────────────────────────────────
    if profile.get("force_trade"):
        _force_fill(ticker, ib, contract, orng, direction, cfg, profile,
                    account_summary, state, state_lock, session_end, currency)
    else:
        send_message(f"😴 {ticker} ORB — no clean setup by {US_ORB_WINDOW_END} NL, standing aside.")


def _monitor_bracket(ib, contract, trades, direction, bracket, orng,
                     ticker, currency, session_end) -> None:
    """Monitor open bracket until TP/SL/re-entry/EOD."""
    parent_trade, tp_trade, sl_trade = trades
    entry       = bracket["entry"]
    take_profit = bracket["take_profit"]
    stop        = bracket["stop"]

    while datetime.now(NL) < session_end:
        ib.sleep(5)

        if tp_trade.orderStatus.status == "Filled":
            fill   = tp_trade.orderStatus.avgFillPrice
            profit = abs(fill - entry) * bracket["qty"]
            send_message(f"🎯 TP hit! +{profit:.2f} {currency} [{ticker}]")
            return

        if sl_trade.orderStatus.status == "Filled":
            fill = sl_trade.orderStatus.avgFillPrice
            loss = abs(fill - entry) * bracket["qty"]
            send_message(f"❌ SL hit. -{loss:.2f} {currency} [{ticker}]")
            return

        bar_raw = ibkr_connector.get_latest_closed_1min_bar(ib, contract)
        if bar_raw is not None:
            breakout_level = orng.high if direction == "long" else orng.low
            reentered = (bar_raw.close < breakout_level if direction == "long"
                         else bar_raw.close > breakout_level)
            if reentered:
                ibkr_connector.cancel_order(ib, tp_trade)
                ibkr_connector.cancel_order(ib, sl_trade)
                ib.sleep(1)
                ibkr_connector.close_position_at_market(ib, contract, direction, bracket["qty"])
                send_message(f"🚪 {ticker} re-entered range — exited at market.")
                return

    ibkr_connector.cancel_order(ib, tp_trade)
    ibkr_connector.cancel_order(ib, sl_trade)
    ib.sleep(1)
    ibkr_connector.close_position_at_market(ib, contract, direction, bracket["qty"])
    send_message(f"⏰ {ticker} EOD flatten — position closed at market.")


# ── EOD safety net ───────────────────────────────────────────────────────────

def _eod_safety_flatten(ib, actionable: list) -> None:
    """Close any positions still open after all ticker threads have finished.

    Catches the edge case where a thread raised an unhandled exception after
    placing a bracket but before the EOD flatten inside _monitor_bracket ran.
    """
    try:
        ib.sleep(2)
        positions = ib.positions()
        watchlist = {p["ticker"] for p in actionable}
        closed = 0
        for pos in positions:
            sym = pos.contract.symbol
            if sym in watchlist and pos.position != 0:
                direction = "long" if pos.position > 0 else "short"
                qty = abs(int(pos.position))
                ibkr_connector.close_position_at_market(
                    ib, pos.contract, direction, qty
                )
                send_message(f"⏰ EOD safety flatten: {sym}  qty={qty}")
                closed += 1
        if closed == 0:
            print("  EOD safety check: no open positions.")
    except Exception as e:
        print(f"[EOD safety flatten] {e}")


if __name__ == "__main__":
    # Delegate to the unified dispatcher — avoids duplicating orchestration logic.
    import live_engine
    live_engine.main()
