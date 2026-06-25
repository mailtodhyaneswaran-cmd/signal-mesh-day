"""
day_trading_prompts.py — Phase 1 intraday AI screener prompts.

TODO (Phase 1):
  - 25 intraday prompts across 5 categories (momentum, fundamentals,
    sentiment, macro, quant) outputting LONG / SHORT / NOTHING
  - Data contract: gap, premarket H/L, RVOL, ATR, float, short%,
    prior levels, SPY/QQQ premarket, VIX, sector, news headlines,
    macro events, USD capital + risk budget
  - Run across Claude + Mistral (2 agents) per pick, then cross-pollination

Placeholder — import this module once prompts are wired.
"""

# Signal values for day-trading (distinct from swing BUY/SELL/HOLD)
LONG    = "LONG"
SHORT   = "SHORT"
NOTHING = "NOTHING"

# Category weights (also in config.INTRADAY_PARAMS — keep in sync)
CATEGORY_WEIGHTS = {
    "momentum":      0.25,
    "fundamentals":  0.20,
    "sentiment":     0.20,
    "macro":         0.15,
    "quant":         0.20,
}

# Phase 1 TODO: populate prompt registry
DAY_TRADING_PROMPTS: dict[str, dict[str, str]] = {
    "momentum":     {},   # M1..M5
    "fundamentals": {},   # F1..F5
    "sentiment":    {},   # S1..S5
    "macro":        {},   # MA1..MA5
    "quant":        {},   # Q1..Q5
}
