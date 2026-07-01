"""
profiles.py — Trading profile registry for Signal Mesh Day.

A profile is a named set of gate overrides that changes how aggressively the
screener and live engine behave.  Flip config.PROFILE to switch posture;
every gate reads config.ACTIVE_PROFILE — nothing needs to be hand-edited in
multiple places.

Available profiles
------------------
  "strict_officer"  Current defaults.  Every gate enforced at full strength.
                    Correct but currently almost never fires.

  "snake_senthil"   Balanced opportunist.  Softer RVOL floor, lower direction
                    threshold, runs the full ORB → IB → VWAP waterfall before
                    placing a forced half-size fill as a last resort.

  "try_luck"        Gambler.  Every RVOL/range/retest gate disabled.  All 5
                    shortlisted tickers traded.  Forces a bracket order at the
                    deadline if nothing triggered naturally.  Every Telegram
                    message is tagged so forced fills are never mistaken for
                    genuine signals.

Keys
----
  bypass_rvol_screener_veto  bool   Skip the screener hard-veto entirely.
  rvol_hard_floor_override   float  Replace rvol_hard_floor (None = use config).
  live_rvol_gate_override    float  Replace orb_rvol_gate on breakout candle
                                    (0.0 = disabled; None = use config).
  direction_threshold_override float  Replace direction_threshold (None = config).
  force_direction_on_nothing bool   If AI returns NOTHING, force direction from
                                    gap sign instead of dropping the ticker.
  respect_cross_poll_override bool  Honor cross-poll "all → NOTHING" consensus.
  max_picks                  int    Watchlist picks sent to the live engine.
  allow_sit_out              bool   Honor SIT_OUT from regime detector.
  require_retest             bool   Require breakout retest before entry
                                    (None = use config.require_retest).
  min_range_pct_override     float  Replace min_range_pct for opening range
                                    (None = use config; 0.0 = disabled).
  force_trade                bool   Place a synthetic order at the deadline
                                    when nothing triggered naturally.
  use_fallback_waterfall     bool   After ORB window expires with no trade,
                                    try IB → VWAP before forcing a fill.
  force_fill_risk_multiplier float  Scale risk_usd for forced/synthetic orders.
  telegram_tag               str    Appended to every Telegram message; None for
                                    normal (untagged) messages.
"""
from __future__ import annotations

_PROFILES: dict[str, dict] = {

    "strict_officer": {
        "bypass_rvol_screener_veto":    False,
        "rvol_hard_floor_override":     None,   # uses config.rvol_hard_floor
        "live_rvol_gate_override":      None,   # uses config.orb_rvol_gate
        "direction_threshold_override": None,   # uses config.direction_threshold
        "force_direction_on_nothing":   False,
        "respect_cross_poll_override":  True,
        "max_picks":                    3,
        "allow_sit_out":                True,
        "require_retest":               None,   # uses config.require_retest
        "min_range_pct_override":       None,   # uses config.min_range_pct
        "force_trade":                  False,
        "use_fallback_waterfall":       False,
        "force_fill_risk_multiplier":   1.0,
        "telegram_tag":                 None,
    },

    "snake_senthil": {
        "bypass_rvol_screener_veto":    False,
        "rvol_hard_floor_override":     0.8,    # soft floor — drops truly dead tickers
        "live_rvol_gate_override":      0.8,
        "direction_threshold_override": 0.05,
        "force_direction_on_nothing":   False,
        "respect_cross_poll_override":  True,
        "max_picks":                    5,
        "allow_sit_out":                False,  # forces VWAP as last resort
        "require_retest":               None,   # first attempt still requires retest
        "min_range_pct_override":       0.00075,  # half of default 0.0015
        "force_trade":                  True,   # only after full waterfall
        "use_fallback_waterfall":       True,
        "force_fill_risk_multiplier":   0.5,
        "telegram_tag":                 "SNAKE FALLBACK",
    },

    "try_luck": {
        "bypass_rvol_screener_veto":    True,
        "rvol_hard_floor_override":     0.0,    # effectively disabled
        "live_rvol_gate_override":      0.0,    # disabled
        "direction_threshold_override": 0.0,    # effectively disabled
        "force_direction_on_nothing":   True,
        "respect_cross_poll_override":  False,
        "max_picks":                    5,
        "allow_sit_out":                False,
        "require_retest":               False,
        "min_range_pct_override":       0.0,    # disabled
        "force_trade":                  True,
        "use_fallback_waterfall":       False,  # force immediately, no waterfall
        "force_fill_risk_multiplier":   1.0,
        "telegram_tag":                 "TRY_LUCK FORCED — not a genuine signal",
    },
}


def get_profile(name: str) -> dict:
    """Return the profile dict for *name*.

    Raises KeyError on an unknown name — same pattern as STRATEGY_REGISTRY in
    strategy_base.py so the error is loud and caught at startup.
    """
    if name not in _PROFILES:
        available = list(_PROFILES)
        raise KeyError(
            f"Unknown trading profile {name!r}. "
            f"Available: {available}"
        )
    return _PROFILES[name]
