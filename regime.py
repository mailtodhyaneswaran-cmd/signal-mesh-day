"""
regime.py — Market regime detection and strategy selection.

Scores four market signals at premarket (~9:00 ET) to pick the best
strategy for the day.  Highest score wins; on a tie the preference
order is VWAP > IB > ORB (VWAP is most robust to data gaps).

Public API
──────────
  pick_strategy(mkt_ctx, candidates, params) -> (str, RegimeScore)
      Returns ("ORB" | "IB" | "VWAP" | "SIT_OUT", RegimeScore)

  next_strategy(current: str) -> str
      Runtime fallback: ORB → IB → VWAP → SIT_OUT

  FALLBACK_WINDOW_NL: dict mapping strategy → NL time cutoff
      After this time, if strategy hasn't triggered, call next_strategy().

Scoring table (from todo_master.md):

  Signal                  ORB   IB   VWAP
  Gap > 3 %               +2   +1    0
  Gap 1–3 %               +1   +2    0
  Gap < 1 %                0    0   +2
  Futures strongly dir.   +1   +1    0   (|SPY pct| >= 0.5 %)
  Futures flat             0    0   +2   (|SPY pct| < 0.3 %)
  VIX > 20                +1   +1    0
  VIX < 15                 0    0   +2
  RVOL > 3x               +2   +1    0
  RVOL 1.5–3x             +1   +2    0
  RVOL < 1.5x              0    0   +2
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ── Regime score ──────────────────────────────────────────────────────────────

@dataclass
class RegimeScore:
    orb:  int = 0
    ib:   int = 0
    vwap: int = 0

    def winner(self) -> str:
        """Return the highest-scoring strategy.  Tie-break: VWAP > IB > ORB."""
        best = max(self.orb, self.ib, self.vwap)
        if best == 0:
            return "SIT_OUT"
        if self.vwap == best:
            return "VWAP"
        if self.ib == best:
            return "IB"
        return "ORB"

    def __str__(self) -> str:
        return f"ORB={self.orb}  IB={self.ib}  VWAP={self.vwap}"


# ── Main scoring function ─────────────────────────────────────────────────────

def pick_strategy(
    mkt_ctx:    dict,
    candidates: list[dict],
    params,
) -> tuple[str, RegimeScore]:
    """
    Score market conditions and return (strategy_name, RegimeScore).

    Args:
        mkt_ctx:    Output of premarket_data.fetch_market_context()
                    Keys: spy_premarket_pct, vix, qqq_premarket_pct, etc.
        candidates: List of candidate dicts.  Uses 'gap_pct' and 'rvol'
                    (rvol may be absent if enrichment hasn't run yet).
        params:     config.INTRADAY_PARAMS (SimpleNamespace).
    """
    score = RegimeScore()

    # ── Signal 1: Gap size (median across candidates) ─────────────────────
    gaps = [abs(float(c.get("gap_pct", 0))) for c in candidates]
    avg_gap = sum(gaps) / len(gaps) if gaps else 0.0

    if avg_gap > 3.0:
        score.orb += 2; score.ib += 1
    elif avg_gap >= 1.0:
        score.orb += 1; score.ib += 2
    else:
        score.vwap += 2

    # ── Signal 2: Futures direction (SPY premarket %) ────────────────────
    spy_pct = _parse_pct(mkt_ctx.get("spy_premarket_pct"))
    if spy_pct is not None:
        if abs(spy_pct) >= 0.5:
            score.orb += 1; score.ib += 1
        elif abs(spy_pct) < 0.3:
            score.vwap += 2

    # ── Signal 3: VIX level ───────────────────────────────────────────────
    vix = _parse_float(mkt_ctx.get("vix"))
    if vix is not None:
        if vix > 20:
            score.orb += 1; score.ib += 1
        elif vix < 15:
            score.vwap += 2

    # ── Signal 4: RVOL (median across candidates, if available) ──────────
    rvol_floor = getattr(params, "rvol_hard_floor",     1.5)
    rvol_full  = getattr(params, "rvol_full_conviction", 3.0)

    rvols = [float(c["rvol"]) for c in candidates if "rvol" in c and c["rvol"] is not None]
    if rvols:
        avg_rvol = sum(rvols) / len(rvols)
        if avg_rvol >= rvol_full:
            score.orb += 2; score.ib += 1
        elif avg_rvol >= rvol_floor:
            score.orb += 1; score.ib += 2
        else:
            score.vwap += 2
    else:
        # No RVOL data → conservatively favour VWAP (doesn't need premarket data)
        score.vwap += 2

    return score.winner(), score


# ── Runtime fallback chain ────────────────────────────────────────────────────

_FALLBACK_CHAIN = {"ORB": "IB", "IB": "VWAP", "VWAP": "SIT_OUT"}

# NL times: if chosen strategy hasn't triggered by this time, cascade down.
#   ORB  no trigger by 16:00 NL (10:00 ET) → try IB
#   IB   no trigger by 17:00 NL (11:00 ET) → try VWAP
#   VWAP no edge by   20:00 NL (14:00 ET)  → sit out
FALLBACK_WINDOW_NL: dict[str, str] = {
    "ORB":  "16:00",
    "IB":   "17:00",
    "VWAP": "20:00",
}


def next_strategy(current: str) -> str:
    """Return the next strategy in the fallback chain.

    ORB → IB → VWAP → SIT_OUT
    """
    return _FALLBACK_CHAIN.get(current, "SIT_OUT")


# ── Private helpers ───────────────────────────────────────────────────────────

def _parse_pct(s) -> float | None:
    """Parse '+0.14%' or '-0.21%' → float.  Returns None on failure."""
    if not s or s == "n/a":
        return None
    try:
        return float(str(s).replace("%", "").replace("+", "").strip())
    except Exception:
        return None


def _parse_float(s) -> float | None:
    if not s or s == "n/a":
        return None
    try:
        return float(str(s).replace("+", "").strip())
    except Exception:
        return None
