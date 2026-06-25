"""
orb_strategy.py — live 5-min Opening Range Breakout engine.

Implements Strategy (strategy_base.py) and the full session runner that
loads watchlist_YYYYMMDD.json, gates direction per the mesh signal, and
executes ORB via orb_core primitives through ibkr_connector.

Signal → Direction mapping (from todo.md):
  BUY  → long  (upside breakout only)
  SELL → short (downside breakout only)
  HOLD → skip

Run via Task Scheduler at ~15:25 NL:
  python orb_strategy.py

Phase 2 TODO:
  - [ ] Multi-ticker concurrent monitoring (threading per pick)
  - [ ] Per-day state.json tracking (MAX_TRADES_PER_SYMBOL_PER_DAY gate)
  - [ ] Hard EOD flatten for all open positions
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
import ibkr_connector
from orb_core import ORBConfig, Bar, OpeningRange, capture_opening_range, detect_breakout, confirm_retest, build_bracket
from strategy_base import StrategySignal, STRATEGY_REGISTRY
from telegram_notify import send_message

NL = ZoneInfo("Europe/Amsterdam")

# Signal → direction mapping
_SIGNAL_TO_DIRECTION = {"BUY": "long", "SELL": "short", "HOLD": "skip"}

# US session window (NL / Amsterdam time)
US_OPEN        = "15:30"
US_OPEN_END    = "15:35"   # opening range closes at this time
US_SESSION_END = "22:00"   # hard EOD flatten


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

def _risk_usd() -> float:
    """Current per-trade risk budget in USD (converts EUR risk at live FX rate)."""
    risk_eur  = config.HOUSE_MONEY_EUR * config.RISK_PER_TRADE_PCT
    eurusd    = ibkr_connector.get_eurusd_rate()
    return risk_eur * eurusd


def _today_at(hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.now(NL).replace(hour=h, minute=m, second=0, microsecond=0)


def _wait_until(target: datetime) -> None:
    while True:
        remaining = (target - datetime.now(NL)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 30))


def _load_watchlist() -> list[dict]:
    """Load today's watchlist_YYYYMMDD.json from WATCHLIST_DIR."""
    today = datetime.now(NL).strftime("%Y%m%d")
    path  = Path(config.WATCHLIST_DIR) / f"watchlist_{today}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Watchlist not found: {path}\n"
            f"Run the Phase 1 screener first."
        )
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


# ── Session runner (single ticker) ───────────────────────────────────────────

def run_ticker(pick: dict, ib, state: dict) -> None:
    """Run the full ORB session for one watchlist pick."""
    ticker    = pick["ticker"]
    signal    = pick.get("signal", "HOLD")
    direction = _SIGNAL_TO_DIRECTION.get(signal, "skip")
    currency  = pick.get("currency", "USD")
    cfg       = ORBConfig.from_params(config.INTRADAY_PARAMS)
    session_end = _today_at(US_SESSION_END)

    if direction == "skip":
        send_message(f"😴 {ticker} — HOLD signal, skipping ORB today.")
        return

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
               f"({opening_bar.low:.2f}–{opening_bar.high:.2f}) — skipping.")
        print(msg); send_message(msg)
        return

    send_message(
        f"📊 <b>{ticker}</b> opening range: {orng.low:.2f}–{orng.high:.2f} {currency}  "
        f"·  bias: {direction.upper()}"
    )

    # ── Poll for breakout → retest ────────────────────────────────────────────
    direction_confirmed = None
    polls_per_sec       = config.INTRADAY_PARAMS.poll_interval_sec

    while datetime.now(NL) < session_end:
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
                lvl_str = orng.high if direction == "long" else orng.low
                send_message(
                    f"{'📈' if direction == 'long' else '📉'} <b>{ticker}</b> "
                    f"broke {'above' if direction == 'long' else 'below'} {lvl_str:.2f}  "
                    f"RVOL {rv:.1f}x — watching for retest..."
                )
        else:
            retest = confirm_retest(orng, bar, cfg, direction_confirmed)
            if retest is False:
                send_message(f"⚠️ {ticker} failed retest — skipping today.")
                return
            if retest is True:
                entry_price = orng.high if direction_confirmed == "long" else orng.low
                bracket     = build_bracket(orng, entry_price, direction_confirmed, cfg, _risk_usd())
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
                state["trades"][ticker] = True
                _save_state(state)
                _monitor_bracket(ib, contract, trades, direction_confirmed,
                                 bracket, orng, ticker, currency, session_end)
                return

        time.sleep(polls_per_sec)

    send_message(f"😴 {ticker} — no clean ORB setup today, window closed.")


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


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"\n{'='*55}")
    print(f"  Signal Mesh Day — ORB Engine")
    print(f"  {datetime.now(NL).strftime('%Y-%m-%d %H:%M')} NL")
    print(f"  LIVE_TRADING = {config.LIVE_TRADING}")
    print(f"{'='*55}\n")

    picks = _load_watchlist()
    actionable = [p for p in picks if _SIGNAL_TO_DIRECTION.get(p.get("signal", "HOLD"), "skip") != "skip"]

    if not actionable:
        send_message("😴 No actionable picks in today's watchlist (all HOLD).")
        return

    send_message(
        f"🚀 <b>ORB engine starting</b>  ·  {len(actionable)} pick(s) to watch\n"
        + "\n".join(
            f"  {'📈' if p.get('signal') == 'BUY' else '📉'} {p['ticker']}  "
            f"{_SIGNAL_TO_DIRECTION.get(p.get('signal','HOLD'),'skip').upper()}  "
            f"RVOL {p.get('rvol', '?')}x  ·  {p.get('catalyst', '')}"
            for p in actionable
        )
    )

    try:
        ib = ibkr_connector.connect()
    except Exception as e:
        msg = f"🔴 IBKR connection failed: {e}\nIs TWS/Gateway running?"
        print(msg); send_message(msg)
        return

    state = _load_state()

    # Phase 2 TODO: run tickers concurrently (threading)
    for pick in actionable[:config.MAX_CONCURRENT_POSITIONS]:
        run_ticker(pick, ib, state)

    ib.disconnect()
    send_message("✅ ORB session complete — all positions flat.")
    print("\nDisconnected cleanly.")


if __name__ == "__main__":
    main()
