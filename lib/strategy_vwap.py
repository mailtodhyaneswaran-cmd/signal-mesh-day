"""
strategy_vwap.py — VWAP Reversion strategy.

VWAP (Volume Weighted Average Price) is the average price of all trades
today, weighted by volume. It resets at 9:30 ET. Price gravitates back
to VWAP — overextensions above/below VWAP tend to mean-revert.

Strategy logic
──────────────
  1. Compute running VWAP from all 1-min bars since open.
  2. When price closes more than vwap_min_deviation % away from VWAP,
     flag a potential reversion entry.
  3. Optionally wait for a reversal candle (close moving back toward VWAP).
  4. Entry at current close; stop at the recent extreme; target at VWAP
     (or tp_r_multiple × risk if VWAP is too close).

Works best on: choppy / range-bound days — exactly when ORB and IB fail.
Does NOT need premarket RVOL data; all signals come from live session bars.

Key differences from ORB/IB
────────────────────────────
  - Mean-reversion (fade the extension), not breakout (follow the move)
  - No fixed opening range; VWAP is computed continuously from bar 1
  - Can fire multiple times per session (not capped to 1 per the code,
    but MAX_TRADES_PER_SYMBOL_PER_DAY = 1 in config still applies)
  - Narrower average stop → more shares per trade at the same dollar risk

VWAP formula (typical/price weighting):
  VWAP = sum((H + L + C) / 3 × V) / sum(V)

All IBKR access goes through ibkr_connector.py. All engine plumbing shared
with the other strategies lives in session_runtime.py.

Called by bin/live_engine.py via run(); never run directly.
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402

import statistics
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

import config
import ibkr_connector
from profiles import get_profile
from session_runtime import (
    _SIGNAL_TO_DIRECTION,
    _risk_usd, _max_notional, _wait_until,
    _et_today_at, _et_nl_hhmm,
    ET_OPEN, ET_VWAP_WINDOW_END, ET_SESSION_END,
    _save_state, _force_fill, _monitor_bracket,
)
from strategy_orb import Bar, OpeningRange, ORBConfig, position_size_usd
from telegram_notify import send_message

NL = ZoneInfo("Europe/Amsterdam")


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class VWAPConfig:
    """All tunable VWAP-reversion knobs. Read from config.INTRADAY_PARAMS."""
    min_deviation:       float = 0.015   # 1.5 % from VWAP to trigger
    tp_r_multiple:       float = 1.5     # more conservative than ORB (2R)
    require_reversal:    bool  = True     # wait for a bar closing back toward VWAP
    warmup_bars:         int   = 15       # bars before looking for trades
    stop_atr_mult:       float = 1.0     # stop = extreme ± atr_mult × bar range
    min_atr_bars:        int   = 10       # bars for ATR estimate
    slippage_pct:        float = 0.0002
    commission_per_share: float = 0.005

    @classmethod
    def from_params(cls, p) -> "VWAPConfig":
        return cls(
            min_deviation        = getattr(p, "vwap_min_deviation",      0.015),
            tp_r_multiple        = getattr(p, "vwap_tp_r_multiple",      1.5),
            require_reversal     = getattr(p, "vwap_require_reversal",   True),
            warmup_bars          = getattr(p, "vwap_warmup_bars",         15),
            stop_atr_mult        = getattr(p, "vwap_stop_atr_mult",      1.0),
            min_atr_bars         = getattr(p, "vwap_min_atr_bars",        10),
            slippage_pct         = getattr(p, "slippage_pct",             0.0002),
            commission_per_share = getattr(p, "commission_per_share",     0.005),
        )


# ── VWAP computation ──────────────────────────────────────────────────────────

def compute_vwap(bars: Sequence[Bar]) -> float:
    """Compute VWAP from a sequence of 1-min bars (typical-price weighting).

    VWAP = sum((H + L + C) / 3 × V) / sum(V)
    Returns 0.0 if bars are empty or total volume is zero.
    """
    cum_pv  = sum((b.high + b.low + b.close) / 3.0 * b.volume for b in bars)
    cum_vol = sum(b.volume for b in bars)
    return cum_pv / cum_vol if cum_vol > 0 else 0.0


def _atr_estimate(bars: Sequence[Bar]) -> float:
    """Simple ATR estimate: mean of last N bar high-low ranges."""
    ranges = [b.high - b.low for b in bars if b.high > b.low]
    return statistics.mean(ranges) if ranges else 0.01


# ── Signal detection ──────────────────────────────────────────────────────────

def detect_vwap_setup(
    bars:  Sequence[Bar],
    cfg:   VWAPConfig,
    bias:  str = "auto",
) -> tuple[Optional[str], float, float]:
    """
    Check if the latest bar is a valid VWAP reversion entry.

    Args:
        bars:  All bars from open up to and including the current bar.
        cfg:   VWAPConfig.
        bias:  "long" | "short" | "auto"
               "auto" fades whichever direction is overextended.
               "long" / "short" only enter in that direction
               (used when the premarket screener set a directional bias).

    Returns:
        (direction, entry_price, vwap)  if a setup is found, else (None, 0, 0).
    """
    if len(bars) < cfg.warmup_bars + 1:
        return None, 0.0, 0.0

    vwap = compute_vwap(bars[:-1])   # VWAP excludes the triggering bar
    bar  = bars[-1]

    if vwap <= 0:
        return None, 0.0, 0.0

    deviation = (bar.close - vwap) / vwap

    if bias == "auto":
        if deviation <= -cfg.min_deviation:
            direction = "long"
        elif deviation >= cfg.min_deviation:
            direction = "short"
        else:
            return None, 0.0, 0.0
    else:
        if abs(deviation) < cfg.min_deviation:
            return None, 0.0, 0.0
        # Direction-gated: only fade if price moved the right way
        if bias == "long"  and deviation >= 0:
            return None, 0.0, 0.0
        if bias == "short" and deviation <= 0:
            return None, 0.0, 0.0
        direction = bias

    # Reversal confirmation: last bar must be moving back toward VWAP
    if cfg.require_reversal and len(bars) >= 2:
        prev = bars[-2]
        if direction == "long"  and bar.close <= prev.close:
            return None, 0.0, 0.0
        if direction == "short" and bar.close >= prev.close:
            return None, 0.0, 0.0

    return direction, bar.close, vwap


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
    stagger_index:    int = 0,
) -> bool:
    """Run the full VWAP reversion session for one watchlist pick (own thread).

    Polls 1-min bars from open, computes running VWAP, and enters when
    price deviates >= vwap_min_deviation% away with a reversal confirmation.
    Called by bin/live_engine.py.

    window_end_override / allow_force_fill: see strategy_orb.run().
    Returns True if a trade was placed (real or forced), else False.
    """
    if profile is None:
        profile = get_profile(config.PROFILE)

    ticker      = pick["ticker"]
    signal      = pick.get("signal", "HOLD")
    direction   = _SIGNAL_TO_DIRECTION.get(signal, "skip")
    currency    = pick.get("currency", "USD")
    session_end = _et_today_at(ET_SESSION_END)
    window_end  = window_end_override or _et_today_at(ET_VWAP_WINDOW_END)
    open_nl     = _et_nl_hhmm(ET_OPEN)   # 09:30 ET in NL

    if direction == "skip":
        send_message(f"😴 {ticker} — HOLD signal, skipping VWAP today.")
        return False

    with state_lock:
        if state["trades"].get(ticker):
            return False

    cfg_vwap      = VWAPConfig.from_params(config.INTRADAY_PARAMS)
    polls_per_sec = config.INTRADAY_PARAMS.poll_interval_sec
    contract      = ibkr_connector.get_contract(ticker, "SMART", currency)

    # Fetch all bars since open to seed the VWAP history
    _wait_until(_et_today_at(ET_OPEN))
    seed_raw = ibkr_connector.get_historical_bars(
        ib, contract, "3600 S", "1 min", use_rth=True,
    )
    bar_history: list[Bar] = [
        Bar(
            t=b.date.astimezone(NL).strftime("%H:%M"),
            open=b.open, high=b.high, low=b.low, close=b.close, volume=float(b.volume),
        )
        for b in (seed_raw or [])
        if b.date.astimezone(NL).strftime("%H:%M") >= open_nl
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
        atr_bars = bar_history[-cfg_vwap.min_atr_bars:]
        atr_est  = _atr_estimate(atr_bars)
        slip     = cfg_vwap.slippage_pct * entry_price

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
        return True

    if profile.get("force_trade") and allow_force_fill:
        # Use the last known VWAP level (or mid of bar_history) for the synthetic range
        last_bars  = bar_history[-5:] if len(bar_history) >= 5 else bar_history
        fake_high  = max(b.high for b in last_bars) if last_bars else 0
        fake_low   = min(b.low  for b in last_bars) if last_bars else 0
        fake_orng  = OpeningRange(ticker=ticker, high=fake_high, low=fake_low)
        cfg_orb    = ORBConfig.from_params(config.INTRADAY_PARAMS)
        _force_fill(ticker, ib, contract, fake_orng, direction if direction != "skip" else "long",
                    cfg_orb, profile, account_summary, state, state_lock, session_end, currency)
        return True
    send_message(f"😴 {ticker} VWAP — no setup by {window_end.strftime('%H:%M')} NL.")
    return False


# ── Backtest simulation ────────────────────────────────────────────────────────

@dataclass
class VWAPTradeResult:
    direction:      str
    entry:          float
    stop:           float
    target:         float
    qty:            int
    exit_price:     float
    exit_reason:    str     # "tp" | "sl" | "vwap_touch" | "session_end"
    risk_per_share: float
    r_multiple:     float
    gross_pnl:      float
    commission:     float
    net_pnl:        float
    entry_bar_idx:  int = 0
    vwap_at_entry:  float = 0.0


def simulate_vwap_session(
    bars:        list[Bar],
    capital_usd: float,
    params,
    bias:        str  = "auto",
    verbose:     bool = False,
) -> Optional[VWAPTradeResult]:
    """
    Replay one session and return the first VWAP trade taken, or None.

    Args:
        bars:        RTH 1-min bars from 9:30 ET.
        capital_usd: Risk budget per trade in USD.
        params:      config.INTRADAY_PARAMS.
        bias:        "auto" | "long" | "short".
        verbose:     Print per-bar trace.
    """
    cfg = VWAPConfig.from_params(params)

    for i in range(cfg.warmup_bars, len(bars)):
        window = bars[:i + 1]
        direction, entry, vwap = detect_vwap_setup(window, cfg, bias)
        if direction is None:
            continue

        atr  = _atr_estimate(bars[max(0, i - cfg.min_atr_bars):i])
        slip = cfg.slippage_pct * entry

        if direction == "long":
            entry_filled = entry + slip
            stop         = entry_filled - cfg.stop_atr_mult * atr
            risk         = entry_filled - stop
            target       = entry_filled + cfg.tp_r_multiple * risk
        else:
            entry_filled = entry - slip
            stop         = entry_filled + cfg.stop_atr_mult * atr
            risk         = stop - entry_filled
            target       = entry_filled - cfg.tp_r_multiple * risk

        if risk <= 0:
            continue

        qty = position_size_usd(capital_usd, risk)
        if qty == 0:
            continue

        if verbose:
            print(f"      [{bars[i].t}] VWAP setup: {direction.upper()}"
                  f"  entry={entry_filled:.4f}  stop={stop:.4f}"
                  f"  target={target:.4f}  qty={qty}"
                  f"  VWAP={vwap:.4f}")

        result = _manage_vwap_trade(
            bars, i + 1, direction, vwap,
            entry_filled, stop, target, qty, risk, cfg, verbose,
        )
        result.entry_bar_idx = i
        result.vwap_at_entry = vwap
        return result

    if verbose:
        print("      No VWAP setup found in session.")
    return None


def _manage_vwap_trade(
    bars:      list[Bar],
    start:     int,
    direction: str,
    vwap:      float,
    entry:     float,
    stop:      float,
    target:    float,
    qty:       int,
    risk:      float,
    cfg:       VWAPConfig,
    verbose:   bool,
) -> VWAPTradeResult:
    exit_price, reason = entry, "session_end"

    for idx in range(start, len(bars)):
        bar = bars[idx]
        # Update running VWAP (only bars up to and including current).
        # Index by position (not bars.index(bar), which is O(n) per bar and
        # returns the wrong index if two bars compare equal).
        running_vwap = compute_vwap(bars[:idx + 1])

        if direction == "long":
            hit_sl   = bar.low  <= stop
            hit_tp   = bar.high >= target
            hit_vwap = bar.close >= running_vwap   # price returned to VWAP
        else:
            hit_sl   = bar.high >= stop
            hit_tp   = bar.low  <= target
            hit_vwap = bar.close <= running_vwap

        if hit_sl:
            exit_price, reason = stop,           "sl"
            break
        if hit_tp:
            exit_price, reason = target,         "tp"
            break
        if hit_vwap and reason != "tp":
            exit_price, reason = running_vwap,   "vwap_touch"
            break

    gross      = (exit_price - entry) * qty if direction == "long" else (entry - exit_price) * qty
    commission = cfg.commission_per_share * qty * 2
    net        = gross - commission
    r_multiple = gross / (risk * qty) if risk * qty else 0.0

    if verbose:
        icon = "✅" if net > 0 else "❌"
        print(f"      RESULT: {reason}  gross={gross:+.2f}  net={net:+.2f}"
              f"  R={r_multiple:.2f}  {icon}")

    return VWAPTradeResult(
        direction, entry, stop, target, qty,
        exit_price, reason, risk, r_multiple, gross, commission, net,
    )
