"""
mock_responses.py -- Pre-written AI agent responses for fast testing.

Import MockAgent instead of ClaudeAgent / MistralAgent when you want to
test the aggregation, cross-pollination, watchlist writing, and live-engine
dispatch logic WITHOUT waiting for real API calls.

Scenario baked into the responses: NVDA with a +3.5% gap, analyst upgrade,
RVOL 2.8x (mid-tier), SPY +0.3%, VIX 17.

Expected aggregate outcome: LONG with moderate conviction (sentiment_flow
returns NOTHING because there is no social/options data, which tests that
the aggregation correctly handles partial signal coverage).

Usage in day_orchestrator.py:
    python bin/day_orchestrator.py --tickers NVDA MU --mock
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402

from inc.lib_agents import BaseAgent


# ---------------------------------------------------------------------------
# Pre-written bulk responses — one list of 5 per category
# Each entry matches the JSON schema of the corresponding prompt.
# ---------------------------------------------------------------------------

MOCK_BULK_RESPONSES: dict[str, list[dict]] = {

    "price_action": [
        # PA1 gap_classification
        {"ticker": "?", "prompt_type": "pa_gap_classification",
         "gap_type": "breakaway", "gap_vs_atr": 1.4,
         "signal": "LONG", "conviction": 72, "risk_level": "medium",
         "reasoning": "Breakaway gap >1x ATR backed by analyst upgrade. Continuation odds high."},
        # PA2 key_levels
        {"ticker": "?", "prompt_type": "pa_key_levels",
         "nearest_resistance": 0.0, "nearest_support": 0.0,
         "suggested_entry_zone": 0.0, "invalidation": 0.0, "target": 0.0,
         "signal": "LONG", "conviction": 68, "risk_level": "medium",
         "reasoning": "Price pressing above prior session high with room to next round level."},
        # PA3 premarket_trend
        {"ticker": "?", "prompt_type": "pa_premarket_trend",
         "structure": "trending_up", "overextended": False,
         "signal": "LONG", "conviction": 65, "risk_level": "medium",
         "reasoning": "Higher highs / higher lows in premarket. Not yet overextended vs ATR."},
        # PA4 vwap_anticipation
        {"ticker": "?", "prompt_type": "pa_vwap_anticipation",
         "expected_scenario": "vwap_hold", "orb_watch_note": "Watch for clean hold above ORB high",
         "signal": "LONG", "conviction": 60, "risk_level": "medium",
         "reasoning": "Gap and fresh catalyst suggest price will hold above VWAP on first test."},
        # PA5 open_scenario
        {"ticker": "?", "prompt_type": "pa_open_scenario",
         "playbook": "gap_and_go", "orb_confirm_level": 0.0,
         "signal": "LONG", "conviction": 70, "risk_level": "medium",
         "reasoning": "Strong catalyst + trending premarket = gap-and-go is dominant playbook."},
    ],

    "catalyst": [
        # CAT1 type
        {"ticker": "?", "prompt_type": "cat_type",
         "catalyst_class": "rating", "catalyst_strength": "strong",
         "signal": "LONG", "conviction": 74, "risk_level": "low",
         "reasoning": "Major analyst upgrade with target raise. Strong day-trade catalyst."},
        # CAT2 freshness
        {"ticker": "?", "prompt_type": "cat_freshness",
         "freshness": "fresh", "likely_priced_in": False,
         "signal": "LONG", "conviction": 71, "risk_level": "low",
         "reasoning": "News released overnight, gap still early — catalyst edge intact."},
        # CAT3 direction_alignment
        {"ticker": "?", "prompt_type": "cat_direction",
         "catalyst_implies": "up", "agrees_with_gap": True,
         "signal": "LONG", "conviction": 75, "risk_level": "low",
         "reasoning": "Analyst upgrade substance supports upside; aligns with gap direction."},
        # CAT4 event_risk
        {"ticker": "?", "prompt_type": "cat_event_risk",
         "binary_event_today": False, "event_timing": "none", "avoid_trade": False,
         "signal": "LONG", "conviction": 70, "risk_level": "low",
         "reasoning": "No binary intraday events. Calendar clear for clean directional trade."},
        # CAT5 magnitude
        {"ticker": "?", "prompt_type": "cat_magnitude",
         "expected_move_pct": 3.5, "clears_costs": True,
         "signal": "LONG", "conviction": 68, "risk_level": "medium",
         "reasoning": "3.5% expected range comfortably clears spread + commission costs."},
    ],

    "sentiment_flow": [
        # SF1 news_sentiment
        {"ticker": "?", "prompt_type": "sf_news_sentiment",
         "tone": "positive", "signal": "LONG", "conviction": 55, "risk_level": "medium",
         "reasoning": "Headline tone net positive on the upgrade; no negative cross-currents."},
        # SF2 social_velocity
        {"ticker": "?", "prompt_type": "sf_social_velocity",
         "buzz": "no_data", "contrarian_extreme": False,
         "signal": "NOTHING", "conviction": 20, "risk_level": "high",
         "reasoning": "Social metrics absent. Cannot confirm or deny momentum — lean NOTHING."},
        # SF3 options_positioning
        {"ticker": "?", "prompt_type": "sf_options_positioning",
         "positioning": "no_data",
         "signal": "NOTHING", "conviction": 20, "risk_level": "medium",
         "reasoning": "No premarket options data available. Cannot derive positioning signal."},
        # SF4 gap_sentiment_confirm
        {"ticker": "?", "prompt_type": "sf_gap_confirm",
         "alignment": "confirms",
         "signal": "LONG", "conviction": 58, "risk_level": "medium",
         "reasoning": "Positive news tone aligns with the upside gap — sentiment confirms."},
        # SF5 crowding_risk
        {"ticker": "?", "prompt_type": "sf_crowding_risk",
         "crowding": "moderate", "squeeze_fuel": False,
         "signal": "NOTHING", "conviction": 45, "risk_level": "high",
         "reasoning": "Moderate crowding on a well-known name. Gap may attract fade attempts."},
    ],

    "market_regime": [
        # MR1 index_futures
        {"ticker": "?", "prompt_type": "mr_index_futures",
         "tape": "risk_on", "supports_stock_direction": True,
         "signal": "LONG", "conviction": 62, "risk_level": "medium",
         "reasoning": "SPY +0.3% premarket. Broad tape is risk-on and supports long bias."},
        # MR2 sector_relative
        {"ticker": "?", "prompt_type": "mr_sector_relative",
         "sector_confirms": True, "is_leader": True,
         "signal": "LONG", "conviction": 60, "risk_level": "medium",
         "reasoning": "Tech sector gapping positively. Stock is leading, not just following."},
        # MR3 volatility_regime
        {"ticker": "?", "prompt_type": "mr_volatility_regime",
         "regime": "normal", "stop_should_widen": False,
         "signal": "LONG", "conviction": 58, "risk_level": "medium",
         "reasoning": "VIX 17 in normal range. Clean trending conditions, no whipsaw risk."},
        # MR4 macro_calendar
        {"ticker": "?", "prompt_type": "mr_macro_calendar",
         "high_impact_event_window": "none", "timing_warning": False,
         "signal": "LONG", "conviction": 62, "risk_level": "low",
         "reasoning": "Calendar clear for opening hour. No macro releases to disrupt setup."},
        # MR5 regime_synthesis
        {"ticker": "?", "prompt_type": "mr_regime_synthesis",
         "environment": "trend_friendly",
         "signal": "LONG", "conviction": 60, "risk_level": "medium",
         "reasoning": "Tape, sector, and VIX all favour trend-following. Good ORB environment."},
    ],

    "quant_edge": [
        # QE1 rvol_gate
        {"ticker": "?", "prompt_type": "qe_rvol_gate",
         "rvol": 2.8, "tier": "mid", "in_play": True, "is_hard_veto": False,
         "signal": "LONG", "conviction": 60, "risk_level": "low",
         "reasoning": "RVOL 2.8x — mid-tier (1.5-3x). Stock is active. Conviction capped."},
        # QE2 gap_statistics
        {"ticker": "?", "prompt_type": "qe_gap_statistics",
         "tendency": "continuation", "gap_percentile": "medium",
         "signal": "LONG", "conviction": 63, "risk_level": "medium",
         "reasoning": "3.5% catalyst-backed gap on large-cap tends to continue at open."},
        # QE3 expected_range_atr
        {"ticker": "?", "prompt_type": "qe_expected_range",
         "target_distance": 0.0, "stop_distance": 0.0, "reward_risk": 2.1,
         "signal": "LONG", "conviction": 65, "risk_level": "medium",
         "reasoning": "Room to next level gives 2.1R. Acceptable reward:risk for the setup."},
        # QE4 risk_sizing
        {"ticker": "?", "prompt_type": "qe_risk_sizing",
         "stop_distance": 0.0, "suggested_qty": 0, "dollar_risk": 0.0, "tradeable": True,
         "signal": "LONG", "conviction": 62, "risk_level": "medium",
         "reasoning": "Risk sizing within 1% budget. Position is tradeable at current spread."},
        # QE5 setup_quality
        {"ticker": "?", "prompt_type": "qe_setup_quality",
         "grade": "B", "missing_pillars": ["rvol_below_3x"],
         "signal": "LONG", "conviction": 66, "risk_level": "medium",
         "reasoning": "Grade B: strong catalyst and gap, RVOL mid-tier limits to B not A."},
    ],
}


# ---------------------------------------------------------------------------
# Cross-pollination response
# ---------------------------------------------------------------------------

MOCK_CROSS_POLLINATION = {
    "ticker":           "?",
    "agent":            "mock",
    "revised_signal":   "LONG",
    "confidence_delta": -5,
    "changed_because":  "Sentiment_flow missing data reduces confidence slightly but "
                        "price/catalyst/regime alignment holds LONG.",
    "reasoning":        "After reviewing peers: sentiment gap noted but not enough to "
                        "override the strong catalyst + tape alignment.",
}


# ---------------------------------------------------------------------------
# MockAgent class
# ---------------------------------------------------------------------------

def _detect_category(prompt: str) -> str:
    """Guess the category from bulk prompt text."""
    prompt_lower = prompt.lower()
    if "price_action" in prompt_lower or "pa1" in prompt_lower or "gap classification" in prompt_lower:
        return "price_action"
    if "catalyst" in prompt_lower or "cat1" in prompt_lower:
        return "catalyst"
    if "sentiment_flow" in prompt_lower or "sf1" in prompt_lower or "social" in prompt_lower:
        return "sentiment_flow"
    if "market_regime" in prompt_lower or "mr1" in prompt_lower or "futures" in prompt_lower:
        return "market_regime"
    if "quant_edge" in prompt_lower or "qe1" in prompt_lower or "rvol gate" in prompt_lower:
        return "quant_edge"
    return "price_action"  # fallback


class MockAgent(BaseAgent):
    """Simulated AI agent — returns pre-written responses instantly.

    Implements the same BaseAgent interface as ClaudeAgent / MistralAgent
    so it can be swapped in transparently.

    Use with --mock flag in day_orchestrator.py for fast logic testing
    without waiting for real API calls.
    """

    def __init__(self, name_tag: str = "mock", verbose: bool = False):
        super().__init__(verbose)
        self._tag           = name_tag
        self._prompt_count  = 0

    @property
    def name(self) -> str:
        return self._tag

    def fetch_data(self, prompt: str, timeout: int = 120) -> dict | list:
        self._prompt_count += 1

        # Cross-pollination prompt returns a single dict
        if any(kw in prompt for kw in ("revised_signal", "reconsidering", "peer_summaries")):
            r = dict(MOCK_CROSS_POLLINATION)
            r["agent"] = self._tag
            if self.verbose:
                print(f"[{self._tag.upper()}] cross-poll mock response")
            return r

        # Bulk category prompt returns list of 5
        category = _detect_category(prompt)
        responses = [dict(r) for r in MOCK_BULK_RESPONSES[category]]
        if self.verbose:
            print(f"[{self._tag.upper()}] bulk mock: {category} ({len(responses)} results)")
        return responses

    def display_verbose(self, prompt_input: str, prompt_output: str) -> None:
        print(f"[{self._tag.upper()} MOCK] prompt #{self._prompt_count}")
