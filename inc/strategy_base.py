"""
strategy_base.py — plug-and-play strategy interface.

Live engine and backtester both call strategies ONLY through Strategy.evaluate().
Split of responsibilities:
  Strategy  → decides signal + levels (entry, stop, target)
  Engine    → owns sizing, bracket order, EOD flatten, IBKR plumbing

Adding a new strategy = drop a new file + register it in orb_strategy.py's
STRATEGY_REGISTRY. No engine rewrite needed.
"""
from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable


@dataclass
class StrategySignal:
    """Output of Strategy.evaluate(). Engine fills in qty via position sizing."""
    direction: str        # "long" | "short" | "skip"
    entry:     float = 0.0
    stop:      float = 0.0
    target:    float = 0.0
    qty:       int   = 0  # set by engine; strategy leaves this 0


@runtime_checkable
class Strategy(Protocol):
    """Every concrete strategy must satisfy this interface.

    Args:
        bars:   Sequence of intraday bars — IBKR live feed or cached historical.
        bias:   Premarket directional bias from watchlist.json.
                "long" | "short" | "skip"  (mapped from BUY/SELL/HOLD by screener).
        params: config.INTRADAY_PARAMS (SimpleNamespace) or equivalent.

    Returns:
        StrategySignal with direction="skip" when no valid setup is found.
    """
    name: str

    def evaluate(
        self,
        bars:   Any,
        bias:   str,
        params: Any,
    ) -> StrategySignal:
        ...


# Registry: strategy name → class.  orb_strategy.py registers "orb" on import.
STRATEGY_REGISTRY: dict[str, type] = {}


def get_strategy(name: str) -> type:
    """Look up a strategy class by name. Raises KeyError if not registered."""
    if name not in STRATEGY_REGISTRY:
        raise KeyError(
            f"Unknown strategy '{name}'. "
            f"Registered: {list(STRATEGY_REGISTRY)}"
        )
    return STRATEGY_REGISTRY[name]
