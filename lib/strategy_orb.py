"""
strategy_orb.py — 5-min Opening Range Breakout strategy.

Owns both the pure ORB logic (range capture, breakout, retest, bracket
sizing, backtest simulation) and the live session runner. This is the one
file to read to understand ORB end-to-end — no separate "core" file.

Signal → Direction mapping:
  BUY  → long  (upside breakout only)
  SELL → short (downside breakout only)
  HOLD → skip

All IBKR access goes through ibkr_connector.py. All engine plumbing shared
with the other strategies (sizing, watchlist/state I/O, force-fill, bracket
monitoring, EOD flatten) lives in session_runtime.py.

Called by bin/live_engine.py via run(); never run directly.
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402

import statistics
import threading
import time
from dataclasses import dataclass, field
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
    ET_OPEN, ET_ORB_RANGE_END, ET_ORB_WINDOW_END, ET_SESSION_END,
    _save_state, _force_fill, _monitor_bracket,
)
from telegram_notify import send_message

NL = ZoneInfo("Europe/Amsterdam")


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class Bar:
    t:      str    # "HH:MM" — used for ordering and display only
    open:   float
    high:   float
    low:    float
    close:  float
    volume: float


@dataclass
class ORBConfig:
    """All tunable ORB knobs in one place.

    Live bot and backtest both read from config.INTRADAY_PARAMS so a single
    change moves both. Instantiate via ORBConfig.from_params(config.INTRADAY_PARAMS).
    """
    range_minutes:         int   = 5
    rvol_min:              float = 1.5
    rvol_mode:             str   = "rolling"   # "rolling" | "opening" | "disabled"
    rvol_rolling_window:   int   = 10
    tp_r_multiple:         float = 2.0
    sl_mode:               str   = "range_edge"  # "range_edge" | "atr"
    atr_mult:              float = 1.0
    require_retest:        bool  = True
    retest_tolerance_pct:  float = 0.0005   # 5 bps of mid-range price
    min_range_pct:         float = 0.0015   # skip thin-range days
    slippage_pct:          float = 0.0002
    commission_per_share:  float = 0.005
    # No new breakout entries after this NL time (too late for 2R before close).
    # Set to "" to disable. 16:30 = 4:30 PM CEST per the 9:30 candle algorithm.
    breakout_window_end:   str   = "16:30"

    @classmethod
    def from_params(cls, p) -> "ORBConfig":
        """Build from config.INTRADAY_PARAMS (SimpleNamespace)."""
        return cls(
            range_minutes        = getattr(p, "orb_range_minutes",     5),
            rvol_min             = getattr(p, "orb_rvol_gate",         1.5),
            rvol_mode            = getattr(p, "rvol_mode",             "rolling"),
            rvol_rolling_window  = getattr(p, "rvol_rolling_window",   10),
            tp_r_multiple        = getattr(p, "tp_r_multiple",         2.0),
            sl_mode              = getattr(p, "sl_mode",               "range_edge"),
            atr_mult             = getattr(p, "atr_mult",              1.0),
            require_retest       = getattr(p, "require_retest",        True),
            retest_tolerance_pct = getattr(p, "retest_tolerance_pct",  0.0005),
            min_range_pct        = getattr(p, "min_range_pct",         0.0015),
            slippage_pct         = getattr(p, "slippage_pct",          0.0002),
            commission_per_share = getattr(p, "commission_per_share",  0.005),
            breakout_window_end  = getattr(p, "breakout_window_end",   "16:30"),
        )


@dataclass
class OpeningRange:
    ticker:    str
    high:      float
    low:       float
    rvol:      float = 0.0       # populated by live engine; 0 in backtest range capture
    mid:       float = field(init=False)
    spread:    float = field(init=False)

    def __post_init__(self):
        self.mid    = (self.high + self.low) / 2.0
        self.spread = self.high - self.low


@dataclass
class TradeResult:
    direction:        str
    entry:            float
    stop:             float
    target:           float
    qty:              int
    exit_price:       float
    exit_reason:      str    # "tp" | "sl" | "range_reentry" | "session_end"
    risk_per_share:   float
    r_multiple:       float
    gross_pnl:        float
    commission:       float
    net_pnl:          float
    breakout_bar_idx: int   = 0
    retest_bar_idx:   int   = 0
    rvol_at_breakout: float = 0.0


# ── Shared helpers ────────────────────────────────────────────────────────────

def position_size_usd(
    risk_usd:        float,
    stop_distance:   float,
    max_notional:    float = 0.0,   # 0 = no cap
    entry_price:     float = 0.0,   # required when max_notional > 0
) -> int:
    """Shares = risk_budget_USD / stop_distance, clamped by a notional cap.

    max_notional > 0: qty = min(qty, floor(max_notional / entry_price)).
    Returns 0 if stop_distance ≤ 0 or if 1 share already exceeds risk_usd.
    """
    if stop_distance <= 0 or risk_usd <= 0:
        return 0
    qty = max(0, int(risk_usd / stop_distance))
    if max_notional > 0 and entry_price > 0:
        qty = min(qty, int(max_notional / entry_price))
    return qty


def rvol(
    global_idx:   int,
    all_bars:     Sequence[Bar],
    opening_bars: Sequence[Bar],
    cfg:          ORBConfig,
) -> float:
    """RVOL for all_bars[global_idx] using the configured baseline mode."""
    if cfg.rvol_mode == "disabled":
        return cfg.rvol_min   # always passes gate

    if cfg.rvol_mode == "opening":
        vols = [b.volume for b in opening_bars if b.volume > 0]
        if not vols:
            return 1.0
        base = statistics.median(vols)
        return all_bars[global_idx].volume / base if base > 0 else 1.0

    # "rolling" — median of the N bars before this one in the full bar list
    start    = max(0, global_idx - cfg.rvol_rolling_window)
    lookback = [b.volume for b in all_bars[start:global_idx] if b.volume > 0]
    if not lookback:
        return 1.0
    base = statistics.median(lookback)
    return all_bars[global_idx].volume / base if base > 0 else 1.0


# ── Primitive API (used by run() below and by strategy_ib.py) ────────────────

def capture_opening_range(bars: Sequence[Bar], cfg: ORBConfig, ticker: str = "") -> Optional[OpeningRange]:
    """Build OpeningRange from the first orb_range_minutes candles.

    Returns None if the range is too thin to trade (< min_range_pct of price).
    """
    if not bars:
        return None
    oh       = max(b.high for b in bars)
    ol       = min(b.low  for b in bars)
    price_ref = (oh + ol) / 2.0
    if (oh - ol) < cfg.min_range_pct * price_ref:
        return None
    return OpeningRange(ticker=ticker, high=oh, low=ol)


def detect_breakout(orng: OpeningRange, bar: Bar, direction: str) -> bool:
    """True if bar closes outside the opening range in the direction permitted by bias.

    direction: "long"  → only upside breakout (close > range high)
               "short" → only downside breakout (close < range low)
    """
    if direction == "long":
        return bar.close > orng.high
    if direction == "short":
        return bar.close < orng.low
    return False


def confirm_retest(orng: OpeningRange, bar: Bar, cfg: ORBConfig, direction: str) -> Optional[bool]:
    """Check whether a bar constitutes a valid retest of the breakout level.

    Per the 9:30-candle algorithm:
      LONG  — retest bar must touch orng.high (low <= high + tol)
               close > orng.high → confirmed (True)
               close <= orng.high → failed (False)  ← bar touched but closed back below the level
      SHORT — retest bar must touch orng.low (high >= low - tol)
               close < orng.low  → confirmed (True)
               close >= orng.low → failed (False)

    Returns:
        True   — valid retest → enter trade
        False  — failed retest → skip the rest of the day
        None   — no retest event on this bar yet (level not touched)
    """
    tol = cfg.retest_tolerance_pct * orng.mid

    if direction == "long":
        touched = bar.low  <= orng.high + tol
        if touched:
            return bar.close > orng.high   # True = confirmed, False = failed
    else:
        touched = bar.high >= orng.low - tol
        if touched:
            return bar.close < orng.low    # True = confirmed, False = failed

    return None  # level not yet touched on this bar


def build_bracket(
    orng:            OpeningRange,
    entry:           float,
    direction:       str,
    cfg:             ORBConfig,
    risk_usd:        float,
    max_notional:    float = 0.0,   # 0 = no cap; pass available_funds * pct
) -> dict:
    """Compute bracket levels and position size.

    Returns dict with keys: entry, stop, take_profit, qty, risk_per_share.
    qty=0 means the setup should be skipped (1 share exceeds risk budget or
    notional cap leaves 0 shares).
    """
    slip = cfg.slippage_pct * entry

    if direction == "long":
        entry_filled = entry + slip
        stop         = orng.low
        risk         = entry_filled - stop
        take_profit  = entry_filled + cfg.tp_r_multiple * risk
    else:
        entry_filled = entry - slip
        stop         = orng.high
        risk         = stop - entry_filled
        take_profit  = entry_filled - cfg.tp_r_multiple * risk

    qty = position_size_usd(risk_usd, risk, max_notional, entry_filled) if risk > 0 else 0

    return {
        "entry":          round(entry_filled, 2),
        "stop":           round(stop,         2),
        "take_profit":    round(take_profit,  2),
        "qty":            qty,
        "risk_per_share": round(risk,         4),
    }


# ── Simulation API (used by bin/backtest.py) ──────────────────────────────────

def simulate_session(
    opening_bars: Sequence[Bar],
    post_bars:    Sequence[Bar],
    capital_usd:  float,
    cfg:          ORBConfig,
    bias:         str            = "long",   # "long" | "short"
    warmup_bars:  Sequence[Bar]  = (),
    verbose:      bool           = False,
) -> Optional[TradeResult]:
    """Replay one session and return the single trade taken, or None.

    Direction is gated by `bias` (mechanical for backtest — no AI direction).
    """
    if not post_bars:
        return None

    orng = capture_opening_range(opening_bars, cfg)
    if orng is None:
        if verbose:
            oh = max(b.high for b in opening_bars)
            ol = min(b.low  for b in opening_bars)
            print(f"      → SKIP: range too thin ({ol:.2f}–{oh:.2f})")
        return None

    if verbose:
        print(f"      Range: {orng.low:.2f}–{orng.high:.2f}  spread={orng.spread:.3f}")

    all_bars    = list(warmup_bars) + list(opening_bars) + list(post_bars)
    post_offset = len(warmup_bars) + len(opening_bars)

    direction_confirmed: Optional[str]   = None
    breakout_idx:        int             = 0
    rvol_at_break:       float           = 0.0

    for i, bar in enumerate(post_bars):
        global_idx = post_offset + i

        if direction_confirmed is None:
            if verbose:
                print(f"      [{bar.t}] O:{bar.open:.2f} H:{bar.high:.2f} "
                      f"L:{bar.low:.2f} C:{bar.close:.2f} V:{bar.volume:.0f}")

            # Breakout window: no new entries after cutoff (too late for 2R before close)
            if cfg.breakout_window_end and bar.t >= cfg.breakout_window_end:
                if verbose:
                    print(f"      [{bar.t}] Breakout window closed ({cfg.breakout_window_end}) — standing aside")
                continue

            # "auto" = take whichever side breaks first (true ORB, no pre-set direction)
            if bias == "auto":
                if detect_breakout(orng, bar, "long"):
                    candidate = "long"
                elif detect_breakout(orng, bar, "short"):
                    candidate = "short"
                else:
                    candidate = None
            else:
                candidate = bias if detect_breakout(orng, bar, bias) else None

            if candidate is not None:
                rv = rvol(global_idx, all_bars, opening_bars, cfg)
                if rv >= cfg.rvol_min:
                    direction_confirmed = candidate
                    breakout_idx        = i
                    rvol_at_break       = rv
                    if verbose:
                        print(f"      → BREAKOUT {candidate.upper()} RVOL={rv:.2f}x ✓")
                elif verbose:
                    print(f"      → breakout ignored — RVOL={rv:.2f}x < {cfg.rvol_min}x")
            continue

        # Retest detection
        if verbose:
            print(f"      [{bar.t}] O:{bar.open:.2f} H:{bar.high:.2f} "
                  f"L:{bar.low:.2f} C:{bar.close:.2f} V:{bar.volume:.0f}  | retest?")

        retest = confirm_retest(orng, bar, cfg, direction_confirmed)
        if retest is False:
            if verbose:
                print("      → FAILED RETEST — skip day")
            return None
        if retest is True:
            if verbose:
                print(f"      → RETEST OK at {bar.t} — entering...")
            risk_usd = capital_usd  # in backtest capital = risk budget per position
            bracket  = build_bracket(orng, orng.high if direction_confirmed == "long" else orng.low,
                                     direction_confirmed, cfg, risk_usd)
            if bracket["qty"] == 0:
                if verbose:
                    print("      → SKIP: qty=0 (stop distance too large for budget)")
                return None
            return _manage_trade(
                post_bars, i + 1,
                direction_confirmed, orng,
                bracket, cfg,
                breakout_idx, i, rvol_at_break,
                verbose,
            )

    if verbose:
        print("      → Session ended — no confirmed retest.")
    return None


def _manage_trade(
    bars:          Sequence[Bar],
    start:         int,
    direction:     str,
    orng:          OpeningRange,
    bracket:       dict,
    cfg:           ORBConfig,
    breakout_idx:  int,
    retest_idx:    int,
    rvol_at_break: float,
    verbose:       bool = False,
) -> TradeResult:
    entry       = bracket["entry"]
    stop        = bracket["stop"]
    take_profit = bracket["take_profit"]
    qty         = bracket["qty"]
    risk        = bracket["risk_per_share"]

    if verbose:
        print(f"      ORDER: {direction.upper()}  entry={entry:.2f}  "
              f"stop={stop:.2f}  tp={take_profit:.2f}  qty={qty}")

    exit_price, reason = entry, "session_end"
    for bar in bars[start:]:
        if direction == "long":
            hit_sl    = bar.low   <= stop
            hit_tp    = bar.high  >= take_profit
            reentered = bar.close <  orng.high
        else:
            hit_sl    = bar.high  >= stop
            hit_tp    = bar.low   <= take_profit
            reentered = bar.close >  orng.low

        if hit_sl:
            exit_price, reason = stop,        "sl"
            if verbose: print(f"      → SL at {stop:.2f}")
            break
        if hit_tp:
            exit_price, reason = take_profit, "tp"
            if verbose: print(f"      → TP at {take_profit:.2f}")
            break
        if reentered:
            exit_price, reason = bar.close,   "range_reentry"
            if verbose: print(f"      → Range re-entry exit at {bar.close:.2f}")
            break
    else:
        exit_price = bars[start - 1].close if start > 0 else entry
        reason     = "session_end"
        if verbose: print(f"      → Session end exit at {exit_price:.2f}")

    gross      = (exit_price - entry) * qty if direction == "long" else (entry - exit_price) * qty
    commission = cfg.commission_per_share * qty * 2
    net        = gross - commission
    r_multiple = gross / (risk * qty) if risk * qty else 0.0

    if verbose:
        icon = "✅" if net > 0 else "❌"
        print(f"      RESULT: gross={gross:+.2f}  commission={commission:.2f}"
              f"  net={net:+.2f}  R={r_multiple:.2f}  {icon}")

    return TradeResult(
        direction, entry, stop, take_profit, qty,
        exit_price, reason,
        risk, r_multiple, gross, commission, net,
        breakout_idx, retest_idx, rvol_at_break,
    )


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
    """Run the full ORB session for one watchlist pick (runs in its own thread).

    Called by bin/live_engine.py's dispatcher. Fetches all bars through
    ibkr_connector.py — never touches ib_async directly.

    window_end_override: NL datetime that caps how long to look for a setup
        (used by the fallback supervisor so ORB gives up early and IB can take
        over). None → natural ORB window.
    allow_force_fill: when False, do NOT force-fill at the deadline (the
        fallback supervisor keeps force-fill for the last strategy in the chain).

    Returns True if a trade was placed (real or forced), else False.
    """
    if profile is None:
        profile = get_profile(config.PROFILE)

    ticker    = pick["ticker"]
    signal    = pick.get("signal", "HOLD")
    direction = _SIGNAL_TO_DIRECTION.get(signal, "skip")
    currency  = pick.get("currency", "USD")
    cfg       = ORBConfig.from_params(config.INTRADAY_PARAMS)
    session_end = _et_today_at(ET_SESSION_END)
    window_end  = window_end_override or _et_today_at(ET_ORB_WINDOW_END)
    open_nl     = _et_nl_hhmm(ET_OPEN)   # NL "HH:MM" of the 09:30 ET opening bar

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
        return False

    # Thread-safe MAX_TRADES_PER_SYMBOL_PER_DAY gate
    with state_lock:
        if state["trades"].get(ticker):
            print(f"{ticker}: trade already taken today — skipping.")
            return False

    send_message(
        f"🔔 Watching <b>{ticker}</b> for ORB  ·  bias: {direction.upper()}\n"
        f"  Waiting for {open_nl} NL candle close..."
    )

    # ── Wait for opening range to form ───────────────────────────────────────
    _wait_until(_et_today_at(ET_ORB_RANGE_END))

    contract    = ibkr_connector.get_contract(ticker, "SMART", currency)
    # Poll for the opening bar to appear (IBKR may take a few seconds to serve
    # the just-closed 09:30 ET bar). Each get_opening_range_bar call is a single
    # fast fetch (retries=1), so we retry at the strategy level here.
    opening_bar = None
    for _ in range(12):
        opening_bar = ibkr_connector.get_opening_range_bar(ib, contract, open_nl)
        if opening_bar is not None:
            break
        time.sleep(5)

    if opening_bar is None:
        msg = f"⚠️ {ticker}: opening candle not available — skipping."
        print(msg); send_message(msg)
        return False

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
        will_force = profile.get("force_trade") and allow_force_fill
        msg = (f"😴 {ticker} range too thin "
               f"({opening_bar.low:.2f}–{opening_bar.high:.2f}) — "
               f"{'forcing entry at last price' if will_force else 'skipping'}")
        print(msg); send_message(msg)
        if not will_force:
            return False
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
                        return False
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
                    return True

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
                    return False
                # require_retest disabled: reset and keep watching for the next breakout
                direction_confirmed = None
            elif retest is True:
                entry_price = orng.high if direction_confirmed == "long" else orng.low
                bracket     = build_bracket(orng, entry_price, direction_confirmed, cfg,
                                            _risk_usd(account_summary), _max_notional(account_summary))
                if bracket["qty"] == 0:
                    send_message(f"⚠️ {ticker} setup skipped — qty=0 (stop too wide for risk budget).")
                    return False

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
                return True

        time.sleep(polls_per_sec)

    # ── Window closed ─────────────────────────────────────────────────────────
    if profile.get("force_trade") and allow_force_fill:
        _force_fill(ticker, ib, contract, orng, direction, cfg, profile,
                    account_summary, state, state_lock, session_end, currency)
        return True
    send_message(f"😴 {ticker} ORB — no clean setup by {window_end.strftime('%H:%M')} NL, standing aside.")
    return False
