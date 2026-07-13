"""
session_runtime.py — shared live-engine plumbing used by every strategy.

Strategy-agnostic helpers that lib/strategy_orb.py, lib/strategy_ib.py, and
lib/strategy_vwap.py all need: signal/direction mapping, position sizing,
NL wall-clock waits, watchlist/state I/O, the synthetic force-fill fallback,
and bracket-order monitoring through to TP/SL/re-entry/EOD.

None of this is ORB-specific — it used to live inside lib/orb_strategy.py
and get imported from there by the other strategies, which is why it moved
into its own module.
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
from telegram_notify import send_message

NL = ZoneInfo("Europe/Amsterdam")

# Signal → direction mapping (shared vocabulary across the watchlist schema)
_SIGNAL_TO_DIRECTION = {"BUY": "long", "SELL": "short", "HOLD": "skip"}


def _ts() -> str:
    """Current NL time as HH:MM:SS for inline log timestamps."""
    return datetime.now(NL).strftime("%H:%M:%S")


# ── Sizing ────────────────────────────────────────────────────────────────────

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


# ── Wall clock ────────────────────────────────────────────────────────────────

def _today_at(hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.now(NL).replace(hour=h, minute=m, second=0, microsecond=0)


def _wait_until(target: datetime) -> None:
    while True:
        remaining = (target - datetime.now(NL)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 30))


# ── Watchlist / state I/O ─────────────────────────────────────────────────────

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
    orng,                                 # strategy_orb.OpeningRange
    direction:       str,
    cfg,                                  # strategy_orb.ORBConfig
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
    from strategy_orb import build_bracket

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


# ── Bracket monitor ───────────────────────────────────────────────────────────

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
