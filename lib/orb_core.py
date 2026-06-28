"""
orb_core.py — shared Opening Range Breakout primitives.

Imported by both the live engine (orb_strategy.py) and the backtester
(backtest.py).  No IBKR, no Telegram, no wall clock — pure logic.

Public API
──────────
Primitive API  (live engine calls these one step at a time):
  capture_opening_range(bars, cfg)           -> OpeningRange
  detect_breakout(orng, bar, direction)      -> bool
  confirm_retest(orng, bar, cfg)             -> bool | None  (None = failed retest)
  build_bracket(orng, entry, direction, cfg, risk_usd) -> dict

Simulation API (backtester calls this once per session):
  simulate_session(opening_bars, post_bars, capital_usd, cfg, warmup_bars, verbose)
      -> Optional[TradeResult]

Shared helpers:
  position_size_usd(risk_usd, stop_distance) -> int
  rvol(global_idx, all_bars, opening_bars, cfg) -> float
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional, Sequence


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
    change moves both.  Instantiate via ORBConfig.from_params(config.INTRADAY_PARAMS).
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

def position_size_usd(risk_usd: float, stop_distance: float) -> int:
    """Shares = risk_budget_USD / stop_distance.  Returns 0 if stop_distance ≤ 0."""
    if stop_distance <= 0 or risk_usd <= 0:
        return 0
    return max(0, int(risk_usd / stop_distance))


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


# ── Primitive API (used by live orb_strategy.py) ─────────────────────────────

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
    orng:      OpeningRange,
    entry:     float,
    direction: str,
    cfg:       ORBConfig,
    risk_usd:  float,
) -> dict:
    """Compute bracket levels and position size.

    Returns dict with keys: entry, stop, take_profit, qty, risk_per_share.
    qty=0 means the setup should be skipped (1 share exceeds risk budget).
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

    qty = position_size_usd(risk_usd, risk) if risk > 0 else 0

    return {
        "entry":          round(entry_filled, 2),
        "stop":           round(stop,         2),
        "take_profit":    round(take_profit,  2),
        "qty":            qty,
        "risk_per_share": round(risk,         4),
    }


# ── Simulation API (used by backtest.py) ─────────────────────────────────────

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
