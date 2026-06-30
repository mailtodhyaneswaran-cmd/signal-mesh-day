"""
Signal Mesh — DAY-TRADING prompt set (premarket bias generator)
================================================================

Purpose
-------
This is the intraday counterpart to `analysis_prompts.py` (which is swing-only).
The day-trading orchestrator runs ~09:00 ET (≈15:00 NL), i.e. BEFORE the 09:30 ET
US open. At that moment there is NO opening range, NO realized VWAP, and NO live
TICK yet — only PREMARKET evidence. So every prompt here produces a PREMARKET
DIRECTIONAL BIAS plus the levels to watch. The ORB engine (`orb_core`) then
CONFIRMS that bias live at the open. Two independent confirmations:

    Signal Mesh Day (premarket bias)  ──►  ORB breakout in that direction  ──►  trade

Output vocabulary
-----------------
Every prompt returns one of:  "LONG" | "SHORT" | "NOTHING"
  LONG    -> bias to buy; ORB will only act on an UPSIDE breakout
  SHORT   -> bias to sell short; ORB will only act on a DOWNSIDE breakout
  NOTHING -> stand aside (the correct default; day trading rewards selectivity)

Every prompt ALSO returns a risk read:
  conviction  : 0-100   (how strongly the bias is held)
  risk_level  : "low" | "medium" | "high"
and the level-bearing prompts return suggested_entry_zone / invalidation / target
so the orchestrator can pre-stage the ORB bracket.

5 categories x 5 prompts = 25 (parity with the swing mesh), remapped for intraday:
  PRICE_ACTION   (was Technical)   -> premarket structure, gap, key levels
  CATALYST       (was Fundamental) -> the fresh event driving today's move
  SENTIMENT_FLOW (was Sentiment)   -> news tone, social velocity, positioning
  MARKET_REGIME  (was Macro)       -> futures / SPY / VIX / sector / macro calendar
  QUANT_EDGE     (was Quant)       -> RVOL gate, gap stats, ATR, float, risk sizing

DATA CONTRACT — premarket variables the orchestrator must supply (all available
pre-open from yfinance / IBKR):
  {ticker} {prior_close} {prior_high} {prior_low}
  {premarket_price} {premarket_high} {premarket_low} {premarket_gap_pct}
  {premarket_volume} {avg_premarket_volume_30d} {rvol_premarket}
  {avg_daily_volume} {atr14} {atr_pct} {float_shares} {short_pct_float}
  {round_levels} {news_headlines} {social_metrics} {options_premarket}
  {catalyst_summary} {next_earnings_date}
  {es_futures_pct} {nq_futures_pct} {spy_premarket_pct} {qqq_premarket_pct}
  {sector_etf} {sector_premarket_pct} {peer_gaps} {vix} {vix_change}
  {macro_events_today} {usd_capital} {risk_per_trade_usd}

NOTE: never fabricate data. If a field is empty (e.g. no options data), the prompt
is told to lower conviction and lean NOTHING rather than invent a read.
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402

# ---------------------------------------------------------------------------
# Shared output contract (documented once; each prompt repeats the JSON shape)
# ---------------------------------------------------------------------------
# {
#   "ticker": "...",
#   "prompt_type": "...",
#   "signal": "LONG" | "SHORT" | "NOTHING",
#   "conviction": 0-100,
#   "risk_level": "low" | "medium" | "high",
#   "reasoning": "<=2 sentences",
#   ... category-specific fields ...
# }


# ===========================================================================
# All tunable parameters live in dat/config.py → INTRADAY_PARAMS (SimpleNamespace).
# dat/config.py is the single source of truth — edit values there, not here.


def _get(params, key):
    """Read a value from params whether it is a dict or a SimpleNamespace."""
    if isinstance(params, dict):
        return params[key]
    return getattr(params, key)


def rvol_conviction_cap(rvol, params=None):
    """Tiered RVOL gate, enforced DETERMINISTICALLY by the orchestrator on the
    numeric rvol — do not rely on the LLM to self-veto. Returns the max conviction
    (0-100) allowed for this ticker; 0 means hard veto -> final signal NOTHING.

        rvol < hard_floor          -> 0    (hard NOTHING)
        hard_floor <= rvol < full  -> mid_cap
        rvol >= full               -> 100  (no cap)

    params: config.INTRADAY_PARAMS (SimpleNamespace) or a tuned dict (backtest).
    Defaults to config.INTRADAY_PARAMS when not supplied.
    """
    if params is None:
        import config as _cfg
        params = _cfg.INTRADAY_PARAMS
    if rvol < _get(params, "rvol_hard_floor"):
        return 0
    if rvol < _get(params, "rvol_full_conviction"):
        return _get(params, "rvol_midtier_cap")
    return 100


# ===========================================================================
# 1. PRICE_ACTION  — premarket structure, gap, key levels   (weight 0.30)
# ===========================================================================
PRICE_ACTION_PROMPTS = {

    "PA1_gap_classification": """
You are an intraday price-action trader analysing the PREMARKET gap for {ticker}.
Prior close {prior_close}; premarket price {premarket_price}; gap {premarket_gap_pct}%.
14-day ATR {atr14} ({atr_pct}% of price). Catalyst: {catalyst_summary}.

TASK: Classify the gap and give a directional bias.
- Breakaway (gap > ~1x ATR WITH a real catalyst) -> strongest continuation odds.
- Continuation (gap with trend + catalyst) -> trade with the gap.
- Common (small gap, no catalyst) -> likely fills, low edge -> usually NOTHING.
- Exhaustion (large gap after an extended multi-day run) -> reversal/fade risk.
A gap without a catalyst is a fade trap, not a momentum setup.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "pa_gap_classification",
  "gap_type": "breakaway|continuation|common|exhaustion",
  "gap_vs_atr": 0.0,
  "signal": "LONG",
  "conviction": 70,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "PA2_key_levels": """
You are mapping the battlefield levels for {ticker} before the open.
Prior day H/L/C: {prior_high}/{prior_low}/{prior_close}.
Premarket H/L: {premarket_high}/{premarket_low}. Premarket price {premarket_price}.
Round-number levels: {round_levels}.

TASK: Identify the nearest level the open will fight over, the level that confirms
the move (likely ORB trigger), and the level that invalidates it. Direction bias
follows which side price is pressing into with room to run.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "pa_key_levels",
  "nearest_resistance": 0.00,
  "nearest_support": 0.00,
  "suggested_entry_zone": 0.00,
  "invalidation": 0.00,
  "target": 0.00,
  "signal": "LONG",
  "conviction": 65,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "PA3_premarket_trend": """
You are reading premarket price STRUCTURE for {ticker}.
Premarket price {premarket_price} vs premarket H/L {premarket_high}/{premarket_low};
gap {premarket_gap_pct}%; ATR {atr_pct}%.

TASK: Is premarket trending (higher-highs/lower-lows), basing, or choppy? Flag
OVEREXTENSION: if price has run far beyond key levels with no consolidation, the
open is more likely to reverse — lower conviction or NOTHING. Clean trend into the
open with room to a level = higher conviction in that direction.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "pa_premarket_trend",
  "structure": "trending_up|trending_down|basing|choppy",
  "overextended": false,
  "signal": "LONG",
  "conviction": 60,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "PA4_vwap_anticipation": """
You are anticipating how {ticker} will behave around VWAP after the open (VWAP does
not exist yet premarket; reason about the likely scenario).
Gap {premarket_gap_pct}%; premarket structure implied by price {premarket_price}
vs premarket H/L {premarket_high}/{premarket_low}; catalyst: {catalyst_summary}.

TASK: Will this most likely be a VWAP-HOLD (gap holds, opens drift up off VWAP ->
continuation, favour the gap direction) or a VWAP-REJECT (gap fades back through
the opening level -> fade)? State what ORB should watch at the open.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "pa_vwap_anticipation",
  "expected_scenario": "vwap_hold|vwap_reject|unclear",
  "orb_watch_note": "...",
  "signal": "LONG",
  "conviction": 55,
  "risk_level": "high",
  "reasoning": "..."
}}
""",

    "PA5_open_scenario": """
You are the senior price-action desk head producing the single most likely OPEN
scenario for {ticker}, synthesising gap {premarket_gap_pct}%, premarket H/L
{premarket_high}/{premarket_low}, prior levels {prior_high}/{prior_low}/{prior_close},
catalyst {catalyst_summary}.

TASK: Choose the dominant playbook for the open and the resulting bias:
- gap_and_go (continuation in gap direction)
- gap_fill_fade (reverse toward prior close)
- range_then_break (no edge until a level breaks)
NOTHING if no clean scenario. Give the level ORB must confirm.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "pa_open_scenario",
  "playbook": "gap_and_go|gap_fill_fade|range_then_break|none",
  "orb_confirm_level": 0.00,
  "signal": "LONG",
  "conviction": 68,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",
}


# ===========================================================================
# 2. CATALYST — the fresh event driving today's move        (weight 0.20)
# ===========================================================================
CATALYST_PROMPTS = {

    "CAT1_catalyst_type": """
You are a catalyst analyst for {ticker}. Headlines: {news_headlines}.
Catalyst summary: {catalyst_summary}. Next earnings date: {next_earnings_date}.

TASK: Classify and grade the catalyst. Strong day-trade catalysts: earnings beat/miss,
guidance change, M&A, FDA/clinical, major contract, analyst re-rating. Weak: vague
headlines, sympathy moves, no news. No real catalyst -> lean NOTHING (gaps without
catalysts fade).

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "cat_type",
  "catalyst_class": "earnings|guidance|m&a|regulatory|contract|rating|sympathy|none",
  "catalyst_strength": "strong|moderate|weak|none",
  "signal": "LONG",
  "conviction": 70,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "CAT2_freshness": """
You are assessing whether the catalyst for {ticker} is FRESH or already PRICED IN.
Headlines: {news_headlines}. Premarket gap {premarket_gap_pct}%; premarket move vs
ATR {atr_pct}%.

TASK: Is the news from overnight/this morning (fresh -> edge intact) or 1-3+ days old
and already largely moved (stale -> most of the move is gone, fade risk)? A fresh
catalyst with the gap still early = higher conviction; a stale catalyst on a big gap
= lower conviction or NOTHING.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "cat_freshness",
  "freshness": "fresh|recent|stale",
  "likely_priced_in": false,
  "signal": "LONG",
  "conviction": 60,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "CAT3_direction_alignment": """
You are mapping the catalyst for {ticker} to a trade DIRECTION.
Headlines: {news_headlines}. Catalyst: {catalyst_summary}. Gap {premarket_gap_pct}%.

TASK: Does the catalyst's substance support LONG or SHORT? Beware mismatches: a
headline "beat" with weak guidance can still be a SHORT; a "miss" already crushed can
bounce. If the catalyst's direction conflicts with the gap direction, that is a
warning -> reduce conviction or NOTHING.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "cat_direction",
  "catalyst_implies": "up|down|mixed",
  "agrees_with_gap": true,
  "signal": "LONG",
  "conviction": 66,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "CAT4_event_risk": """
You are screening {ticker} for binary EVENT RISK today.
Next earnings date: {next_earnings_date}. Catalyst: {catalyst_summary}.
Macro events today: {macro_events_today}.

TASK: Is there an unresolved binary event during the session (earnings after close
today, FDA decision, court ruling, investor day) that could whipsaw an intraday
position? Day trades flatten by close, so after-close events are usually fine but
flag them; an intraday binary release = high risk -> size down or NOTHING.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "cat_event_risk",
  "binary_event_today": false,
  "event_timing": "premarket|intraday|after_close|none",
  "avoid_trade": false,
  "signal": "NOTHING",
  "conviction": 50,
  "risk_level": "high",
  "reasoning": "..."
}}
""",

    "CAT5_magnitude": """
You are sizing the EXPECTED MOVE from the catalyst for {ticker}.
Gap {premarket_gap_pct}%; ATR {atr_pct}%; catalyst {catalyst_summary}.

TASK: Is the catalyst big enough to drive an intraday move that clears spread/costs
and reaches a sensible target (compare to ATR)? Tiny expected move vs costs -> NOTHING.
Large, catalyst-justified expected move -> conviction in the catalyst direction.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "cat_magnitude",
  "expected_move_pct": 0.0,
  "clears_costs": true,
  "signal": "LONG",
  "conviction": 62,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",
}


# ===========================================================================
# 3. SENTIMENT_FLOW — news tone, social velocity, positioning  (weight 0.15)
# ===========================================================================
SENTIMENT_FLOW_PROMPTS = {

    "SF1_news_sentiment": """
You are a news-sentiment analyst for {ticker}. REAL premarket/overnight headlines:
{news_headlines}.

TASK: Score the net tone (negative regulatory/legal headlines outweigh several mild
positives). If the headline list is empty, you have NO basis -> conviction <= 20 and
signal NOTHING; do NOT invent sentiment.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "sf_news_sentiment",
  "tone": "positive|negative|neutral|mixed|no_data",
  "signal": "LONG",
  "conviction": 55,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "SF2_social_velocity": """
You are reading retail/social momentum for {ticker}. Metrics: {social_metrics}.

TASK: Is social mention velocity rising with a directional mood, or quiet? Flag a
CONTRARIAN extreme: euphoric, vertical hype on an already-extended name = exhaustion/
fade risk, not confirmation. Rising-but-early buzz aligned with the gap = momentum.
Empty metrics -> low conviction, do not fabricate.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "sf_social_velocity",
  "buzz": "rising|steady|quiet|no_data",
  "contrarian_extreme": false,
  "signal": "NOTHING",
  "conviction": 45,
  "risk_level": "high",
  "reasoning": "..."
}}
""",

    "SF3_options_positioning": """
You are reading premarket options positioning for {ticker}: {options_premarket}.

TASK: If data exists, infer directional lean from call/put skew, unusual sweeps, and
IV. Heavy call buying / bullish skew supports LONG; put skew supports SHORT. If
{options_premarket} is empty, return signal NOTHING with conviction <= 20 — do not
guess.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "sf_options_positioning",
  "positioning": "bullish|bearish|neutral|no_data",
  "signal": "NOTHING",
  "conviction": 30,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "SF4_gap_sentiment_confirm": """
You are checking whether sentiment CONFIRMS the gap for {ticker}.
Gap {premarket_gap_pct}%; headlines {news_headlines}; social {social_metrics}.

TASK: Gap up + positive fresh news + rising buzz = aligned (confirms LONG). Gap up on
neutral/negative or absent news = suspicious (fade risk -> NOTHING or SHORT bias).
Mirror logic for gap downs.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "sf_gap_confirm",
  "alignment": "confirms|contradicts|unclear",
  "signal": "LONG",
  "conviction": 58,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "SF5_crowding_risk": """
You are assessing CROWDING / exhaustion risk for {ticker}.
Social {social_metrics}; gap {premarket_gap_pct}%; move vs ATR {atr_pct}%;
short interest {short_pct_float}% of float.

TASK: Is this a heavily crowded retail name whose move may be late/exhausted? High
crowding + large prior run = fade risk (lower conviction). High short interest + fresh
positive catalyst = squeeze fuel (supports LONG). Distinguish the two.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "sf_crowding_risk",
  "crowding": "high|moderate|low",
  "squeeze_fuel": false,
  "signal": "NOTHING",
  "conviction": 48,
  "risk_level": "high",
  "reasoning": "..."
}}
""",
}


# ===========================================================================
# 4. MARKET_REGIME — futures / SPY / VIX / sector / macro      (weight 0.10)
# ===========================================================================
MARKET_REGIME_PROMPTS = {

    "MR1_index_futures": """
You are reading the broad-tape premarket context.
ES {es_futures_pct}%, NQ {nq_futures_pct}%, SPY premarket {spy_premarket_pct}%,
QQQ premarket {qqq_premarket_pct}%. Stock {ticker} gap {premarket_gap_pct}%.

TASK: Is the market premarket risk-ON (futures up, broad) or risk-OFF? A long bias on
a green-futures morning is with the tape; a long into red futures is fighting it ->
lower conviction. Decide whether the tape SUPPORTS the stock's gap direction.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "mr_index_futures",
  "tape": "risk_on|risk_off|mixed",
  "supports_stock_direction": true,
  "signal": "LONG",
  "conviction": 60,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "MR2_sector_relative": """
You are checking sector/peer confirmation for {ticker} (sector ETF {sector_etf}
premarket {sector_premarket_pct}%; peer gaps {peer_gaps}).

TASK: Is the whole sector gapping the same way (broad theme -> stronger, stock is a
leader) or is {ticker} an isolated mover (sympathy/idiosyncratic -> weaker, or it IS
the leader)? Sector-confirmed direction raises conviction. Also note correlation risk
if several picks are the same sector.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "mr_sector_relative",
  "sector_confirms": true,
  "is_leader": true,
  "signal": "LONG",
  "conviction": 58,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "MR3_volatility_regime": """
You are classifying the volatility regime. VIX {vix} (change {vix_change}).

TASK: Low/falling VIX -> cleaner trend days, tighter stops can work, higher conviction
on directional setups. High/spiking VIX -> whipsaw/chop, wider stops needed, smaller
size, more NOTHING. Translate the regime into a conviction and risk_level modifier for
a directional intraday trade.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "mr_volatility_regime",
  "regime": "calm_trend|normal|elevated_chop|stress",
  "stop_should_widen": false,
  "signal": "LONG",
  "conviction": 55,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "MR4_macro_calendar": """
You are screening today's macro calendar for timing risk.
Events today: {macro_events_today}. (US data often 08:30 & 10:00 ET; Fed 14:00 ET.)

TASK: Are there high-impact releases that will hit shortly after the open (e.g. 10:00
ET data) and whipsaw early positions? If a major release lands in the first trading
hour, flag it: bias toward waiting / smaller size / NOTHING around that window. If the
calendar is clear, no constraint.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "mr_macro_calendar",
  "high_impact_event_window": "open_hour|midday|none",
  "timing_warning": false,
  "signal": "LONG",
  "conviction": 55,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "MR5_regime_synthesis": """
You are the macro desk head giving the GO/NO-GO environment read for a directional
intraday trade in {ticker}, synthesising tape ({spy_premarket_pct}% SPY,
{qqq_premarket_pct}% QQQ), VIX {vix} ({vix_change}), sector {sector_premarket_pct}%,
and macro events {macro_events_today}.

TASK: Is today a trend-friendly environment that supports taking the stock's
directional bias, or a chop/headline environment where standing aside is wiser? Output
the environment-level bias (this gates the trade, it does not pick the stock).

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "mr_regime_synthesis",
  "environment": "trend_friendly|neutral|hostile",
  "signal": "LONG",
  "conviction": 57,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",
}


# ===========================================================================
# 5. QUANT_EDGE — RVOL gate, gap stats, ATR, float, risk      (weight 0.25)
# ===========================================================================
QUANT_EDGE_PROMPTS = {

    "QE1_rvol_gate": """
You are enforcing the TIERED volume gate for {ticker} (the single most important
day-trade filter). Premarket volume {premarket_volume} vs 30d avg premarket
{avg_premarket_volume_30d} -> premarket RVOL {rvol_premarket}x. Avg daily vol
{avg_daily_volume}.

TASK: Apply the three-tier "stock in play" rule (thresholds injected, do not assume):
- RVOL < {rvol_hard_floor}x        -> NOT in play. signal NOTHING, is_hard_veto=true.
                                       Low relative volume = no edge; this VETOES the
                                       ticker regardless of every other prompt.
- {rvol_hard_floor}x <= RVOL < {rvol_full_conviction}x
                                     -> tradeable but second-tier. Keep conviction
                                        modest (the orchestrator will also cap it).
- RVOL >= {rvol_full_conviction}x   -> genuine stock in play, full conviction allowed.
The orchestrator re-checks RVOL deterministically, so report the true tier honestly.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "qe_rvol_gate",
  "rvol": 0.0,
  "tier": "below_floor|mid|in_play",
  "in_play": true,
  "is_hard_veto": false,
  "signal": "LONG",
  "conviction": 75,
  "risk_level": "low",
  "reasoning": "..."
}}
""",

    "QE2_gap_statistics": """
You are applying gap base-rates for {ticker}. Gap {premarket_gap_pct}%; ATR
{atr_pct}%; gap-vs-ATR ratio implied; float {float_shares}.

TASK: Given the gap size percentile and type, what is the tendency — CONTINUATION
(trade with the gap) or FILL (fade toward prior close)? Large catalyst-backed gaps on
lower float tend to continue; small no-news gaps tend to fill. Convert the base-rate
into a directional bias and conviction.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "qe_gap_statistics",
  "tendency": "continuation|fill|mixed",
  "gap_percentile": "small|medium|large|extreme",
  "signal": "LONG",
  "conviction": 60,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "QE3_expected_range_atr": """
You are checking there is enough ROOM to a target for {ticker}.
ATR14 {atr14} ({atr_pct}%); premarket price {premarket_price}; nearest opposing level
implied by prior {prior_high}/{prior_low} and round levels {round_levels}.

TASK: Is the distance to a realistic target (e.g. ~1.0-1.5x ATR or the next level)
large relative to the stop distance, giving an acceptable reward:risk (>= ~2:1)? If
ATR is tiny or price is jammed against a level with no room, conviction drops / NOTHING.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "qe_expected_range",
  "target_distance": 0.00,
  "stop_distance": 0.00,
  "reward_risk": 0.0,
  "signal": "LONG",
  "conviction": 62,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "QE4_risk_sizing": """
You are the risk manager sizing the trade for {ticker}.
Capital {usd_capital} USD (converted from EUR5000 house money); risk budget
{risk_per_trade_usd} USD (~1%). Premarket price {premarket_price}; ATR {atr14};
proposed stop distance from the level/ATR read.

TASK: Compute suggested share quantity = risk_per_trade_usd / stop_distance, and
confirm dollar-risk discipline (size DOWN when the stop is wide — keep dollar risk
constant, never widen risk to fit a position). Flag if spread/illiquidity or a sub-1R
setup makes it untradeable -> NOTHING. This prompt sets the position size, not the
direction (echo the prevailing bias or NOTHING).

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "qe_risk_sizing",
  "stop_distance": 0.00,
  "suggested_qty": 0,
  "dollar_risk": 0.00,
  "tradeable": true,
  "signal": "LONG",
  "conviction": 60,
  "risk_level": "medium",
  "reasoning": "..."
}}
""",

    "QE5_setup_quality": """
You are the desk's statistical grader for {ticker}, combining RVOL {rvol_premarket}x,
gap {premarket_gap_pct}% vs ATR {atr_pct}%, float {float_shares}, short interest
{short_pct_float}%, and catalyst {catalyst_summary}.

TASK: Grade the overall setup A/B/C. A = high RVOL + clean catalyst-backed gap + room +
favourable float. C = missing pillars. Only A/B warrant a trade; grade C -> NOTHING.
Be selective: most premarket names are C.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "prompt_type": "qe_setup_quality",
  "grade": "A|B|C",
  "missing_pillars": [],
  "signal": "LONG",
  "conviction": 70,
  "risk_level": "low",
  "reasoning": "..."
}}
""",
}


# ===========================================================================
# Registry, weights, agent specialisations
# ===========================================================================
ALL_INTRADAY_PROMPTS = {
    "price_action":   PRICE_ACTION_PROMPTS,
    "catalyst":       CATALYST_PROMPTS,
    "sentiment_flow": SENTIMENT_FLOW_PROMPTS,
    "market_regime":  MARKET_REGIME_PROMPTS,
    "quant_edge":     QUANT_EDGE_PROMPTS,
}


def build_bulk_category_prompt(category: str, filled_prompts: list[tuple[str, str]]) -> str:
    """
    Combine all 5 pre-filled prompts from one category into a single API call.

    Args:
        category:       Category name (e.g. "price_action").
        filled_prompts: List of (prompt_key, filled_prompt_text) — already data-substituted.

    Returns:
        A single prompt string asking the LLM to return a JSON array of N objects,
        one per analysis, in order.

    Usage (orchestrator):
        filled = [(key, template.format_map(data)) for key, template in prompts.items()]
        bulk   = build_bulk_category_prompt(category, filled)
        raw    = agent.fetch_data(bulk)   # returns list or dict
    """
    n = len(filled_prompts)
    parts = [
        f"You are an intraday trading analyst. Complete the {n} INDEPENDENT analyses below "
        f"for the [{category.upper()}] category.\n"
        f"Each analysis is self-contained — evaluate each one fully on its own merits.\n\n"
        f"RESPONSE FORMAT — return ONLY a JSON array of exactly {n} objects in order:\n"
        f"[<result_1>, <result_2>, ..., <result_{n}>]\n"
        f"No text, explanation, or markdown outside the JSON array.\n",
    ]
    for i, (key, text) in enumerate(filled_prompts, 1):
        parts.append(f"\n{'─'*52}")
        parts.append(f"## Analysis {i}/{n}: {key}")
        parts.append(text.strip())

    parts.append(f"\n{'─'*52}")
    parts.append(
        f"Return a JSON array of exactly {n} objects "
        f"(one per analysis, same order as above)."
    )
    return "\n".join(parts)

# Category weights live in config.INTRADAY_PARAMS (single source of truth).

# Active agents: 2 for now (Claude + Mistral). Gemini is temporarily dropped — it was
# returning malformed answers or failing/timing out before completing. Both active
# agents still run all 25 prompts; the lists below are only emphasis tags used to
# weight each agent's voice during cross-pollination, so all 5 categories stay covered
# by both.
#
# FUTURE EXPANSION: to go back to 3+ agents, (1) re-add the model here with its
# category emphasis (e.g. "gemini": ["quant_edge", "price_action"]), (2) register its
# backend in the orchestrator's agent factory, and (3) widen data fetching to match
# (see DATA-FETCH note below). The aggregation/cross-pollination logic is agent-count
# agnostic — it sums over whatever agents are present — so no scoring changes are
# needed when the roster grows.
AGENT_SPECIALISATIONS = {
    "claude":  ["price_action", "catalyst", "quant_edge"],
    "mistral": ["sentiment_flow", "market_regime"],
}

# DATA-FETCH SCOPE: provision only the 2 active agents' API clients/keys for now
# (Claude + Mistral). Keep the fetch layer behind a simple agent registry so adding a
# 3rd model later is a config change, not a rewrite. Free-tier/rate-limit handling
# should fail that agent's prompts to SKIP (excluded from the vote pool), never
# substitute a fake vote — same rule as the swing system.


# ===========================================================================
# Aggregation guidance (implemented in the orchestrator, documented here)
# ===========================================================================
# All numbers below come from INTRADAY_PARAMS so the backtest tunes them centrally.
#
# 1. TIERED RVOL GATE first — enforced on the numeric rvol, not the LLM's say-so:
#       cap = rvol_conviction_cap(rvol, params)
#       cap == 0            -> final signal NOTHING (hard veto, skip all scoring)
#       0 < cap < 100       -> mid tier: clamp EVERY prompt's conviction to <= cap
#       cap == 100          -> full conviction allowed
# 2. Map each prompt vote: LONG=+1, SHORT=-1, NOTHING=0.
# 3. Weighted net = sum over prompts of
#       vote * (min(conviction, cap)/100) * category_weights[category]
# 4. Final bias: LONG if net >= +direction_threshold, SHORT if net <= -threshold,
#    else NOTHING.
# 5. Aggregate risk_level: if any HARD-risk prompt (event/binary, stress regime) is
#    "high", cap conviction further and widen the ORB stop accordingly.
# 6. Pass {direction, conviction, suggested_entry_zone, invalidation, target,
#    suggested_qty} into watchlist.json for the ORB engine to CONFIRM at the open.
#
# Reference implementation (orchestrator imports this; backtest passes a tuned params):
def aggregate_ticker(prompt_results, rvol, params=None):
    """Collapse all prompt votes for one ticker into a final bias. `prompt_results`
    is an iterable of dicts with keys: signal, conviction, category.

    params: config.INTRADAY_PARAMS (SimpleNamespace) or a tuned dict (backtest).
    Defaults to config.INTRADAY_PARAMS when not supplied.
    """
    if params is None:
        import config as _cfg
        params = _cfg.INTRADAY_PARAMS
    cap = rvol_conviction_cap(rvol, params)
    if cap == 0:
        return {"direction": "NOTHING", "net": 0.0, "rvol_veto": True}

    vote_map = {"LONG": 1, "SHORT": -1, "NOTHING": 0}
    weights = _get(params, "category_weights")
    net = 0.0
    for r in prompt_results:
        conv = min(r.get("conviction", 0), cap) / 100.0
        net += vote_map.get(r.get("signal", "NOTHING"), 0) * conv * weights.get(r.get("category"), 0)

    thr = _get(params, "direction_threshold")
    direction = "LONG" if net >= thr else "SHORT" if net <= -thr else "NOTHING"
    return {"direction": direction, "net": round(net, 4), "rvol_veto": False}


# ===========================================================================
# Cross-pollination (deliberation) prompt
# ===========================================================================
# Run after round 1. Each agent sees the other agents' per-category conclusions for
# this ticker and may revise. Keeps the multi-agent debate from the swing system but
# in LONG/SHORT/NOTHING terms, and explicitly weights DISAGREEMENT as information.
CROSS_POLLINATION_PROMPT = """
You are {agent_name}, a day-trading analyst reconsidering your PREMARKET bias on
{ticker} for a same-day trade (flat by close).

Your round-1 conclusion:
{own_summary}

The other agents concluded:
{peer_summaries}

Premarket facts (unchanged): gap {premarket_gap_pct}%, RVOL {rvol_premarket}x,
catalyst {catalyst_summary}, tape SPY {spy_premarket_pct}% / VIX {vix}.

TASK:
- If a peer raises a risk you underweighted (e.g. stale catalyst, low RVOL, hostile
  tape, binary event), revise your conviction down or flip to NOTHING.
- Genuine disagreement across agents is itself a signal of low edge — do NOT
  manufacture consensus; when the three of you split, NOTHING is often correct.
- Only hold or raise conviction if the premarket evidence clearly supports it.

Respond ONLY in JSON, no other text:
{{
  "ticker": "{ticker}",
  "agent": "{agent_name}",
  "revised_signal": "LONG|SHORT|NOTHING",
  "confidence_delta": 0,
  "changed_because": "...",
  "reasoning": "..."
}}
"""
